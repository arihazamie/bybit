"""
db/database.py
Koneksi SQLite, inisialisasi tabel, dan seed default settings.

Menggunakan sqlite3 native Python (bukan ORM) sesuai keputusan teknis.
Thread-safe via check_same_thread=False + WAL mode untuk concurrent read.
"""

import asyncio
import logging
import sqlite3
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from db.models import (
    ALL_CREATE_STATEMENTS,
    ALL_TRIGGERS,
    DEFAULT_CIRCUIT_BREAKER_COMPONENTS,
    DEFAULT_SETTINGS,
)

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# Path database
# ─────────────────────────────────────────────
_DEFAULT_DB_PATH = Path("data/bot.db")
_db_path: Path = _DEFAULT_DB_PATH


def set_db_path(path: str | Path) -> None:
    """Override path database (berguna untuk testing)."""
    global _db_path
    _db_path = Path(path)
    logger.info(f"DB path set to: {_db_path}")


def get_db_path() -> Path:
    return _db_path


# ─────────────────────────────────────────────
# Connection factory
# ─────────────────────────────────────────────
def _make_connection(path: Path) -> sqlite3.Connection:
    """
    Buat koneksi SQLite dengan konfigurasi optimal:
    - WAL mode: concurrent read tanpa block write
    - Foreign keys: enforced
    - Row factory: sqlite3.Row untuk akses kolom by name
    - Timeout 30 detik untuk busy wait
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        str(path),
        check_same_thread=False,
        timeout=30.0,
    )
    conn.row_factory = sqlite3.Row

    # Aktifkan WAL mode untuk concurrency lebih baik
    conn.execute("PRAGMA journal_mode=WAL;")
    # Enforce foreign key constraints
    conn.execute("PRAGMA foreign_keys=ON;")
    # Sinkronisasi yang cukup cepat namun aman
    conn.execute("PRAGMA synchronous=NORMAL;")

    return conn


# ─────────────────────────────────────────────
# Context managers (sync)
# ─────────────────────────────────────────────
@contextmanager
def get_db():
    """
    Sync context manager untuk mendapatkan koneksi DB.
    Auto-commit jika tidak ada exception, rollback jika ada.

    Usage:
        with get_db() as conn:
            conn.execute("SELECT ...")
    """
    conn = _make_connection(_db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def get_cursor():
    """
    Sync context manager yang langsung return cursor.

    Usage:
        with get_cursor() as cur:
            cur.execute("SELECT ...")
            rows = cur.fetchall()
    """
    with get_db() as conn:
        cursor = conn.cursor()
        try:
            yield cursor
        finally:
            cursor.close()


# ─────────────────────────────────────────────
# Async wrappers (run sync SQLite in executor)
# ─────────────────────────────────────────────
async def async_execute(sql: str, params: tuple = ()) -> Optional[list]:
    """
    Jalankan satu SQL statement secara async (via thread pool).
    Return list of rows untuk SELECT, None untuk INSERT/UPDATE/DELETE.
    """
    loop = asyncio.get_event_loop()

    def _run():
        with get_db() as conn:
            cur = conn.execute(sql, params)
            if sql.strip().upper().startswith("SELECT"):
                return cur.fetchall()
            return None

    return await loop.run_in_executor(None, _run)


async def async_execute_many(sql: str, params_list: list[tuple]) -> None:
    """Batch insert/update async."""
    loop = asyncio.get_event_loop()

    def _run():
        with get_db() as conn:
            conn.executemany(sql, params_list)

    await loop.run_in_executor(None, _run)


# ─────────────────────────────────────────────
# Inisialisasi database
# ─────────────────────────────────────────────
def init_db(db_path: Optional[str | Path] = None) -> None:
    """
    Inisialisasi database:
    1. Buat semua tabel (idempotent — IF NOT EXISTS)
    2. Buat semua trigger
    3. Seed default settings (jika belum ada)
    4. Seed circuit breaker state (jika belum ada)

    Aman dipanggil berulang kali (idempotent).
    """
    if db_path:
        set_db_path(db_path)

    logger.info(f"Initializing database at: {_db_path}")

    with get_db() as conn:
        # 1. Buat semua tabel
        for stmt in ALL_CREATE_STATEMENTS:
            conn.execute(stmt)
        logger.debug("All tables created/verified")

        # 2. Buat semua trigger
        for trigger in ALL_TRIGGERS:
            conn.execute(trigger)
        logger.debug("All triggers created/verified")

        # 3. Seed default settings
        _seed_default_settings(conn)

        # 4. Seed circuit breaker components
        _seed_circuit_breaker(conn)

    logger.info("Database initialization complete")


def _seed_default_settings(conn: sqlite3.Connection) -> None:
    """
    Insert default settings HANYA jika key belum ada (INSERT OR IGNORE).
    Ini memastikan setting yang sudah diubah user tidak ter-overwrite saat restart.
    """
    now_utc = datetime.now(timezone.utc).isoformat()
    rows_inserted = 0

    for key, value in DEFAULT_SETTINGS.items():
        cur = conn.execute(
            "INSERT OR IGNORE INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
            (key, value, now_utc),
        )
        rows_inserted += cur.rowcount

    if rows_inserted > 0:
        logger.info(f"Seeded {rows_inserted} default settings")
    else:
        logger.debug("All default settings already exist, no seed needed")


def _seed_circuit_breaker(conn: sqlite3.Connection) -> None:
    """
    Insert semua komponen circuit breaker dalam state CLOSED (default).
    INSERT OR IGNORE — jika sudah ada, biarkan state yang ada.
    """
    now_utc = datetime.now(timezone.utc).isoformat()
    rows_inserted = 0

    for component in DEFAULT_CIRCUIT_BREAKER_COMPONENTS:
        cur = conn.execute(
            """INSERT OR IGNORE INTO circuit_breaker_state
               (component, state, consecutive_error_count, updated_at)
               VALUES (?, 'closed', 0, ?)""",
            (component, now_utc),
        )
        rows_inserted += cur.rowcount

    if rows_inserted > 0:
        logger.info(f"Seeded {rows_inserted} circuit breaker components")
    else:
        logger.debug("Circuit breaker components already exist")


# ─────────────────────────────────────────────
# Health check
# ─────────────────────────────────────────────
def check_db_health() -> dict:
    """
    Cek health database. Return dict dengan status dan info.
    Digunakan oleh /status command Telegram.
    """
    try:
        with get_db() as conn:
            # Test koneksi
            cur = conn.execute("SELECT COUNT(*) FROM trades")
            trade_count = cur.fetchone()[0]

            cur = conn.execute("SELECT COUNT(*) FROM signal_log")
            signal_count = cur.fetchone()[0]

            cur = conn.execute("PRAGMA integrity_check")
            integrity = cur.fetchone()[0]

        return {
            "status": "healthy",
            "db_path": str(_db_path),
            "trade_count": trade_count,
            "signal_count": signal_count,
            "integrity": integrity,
        }
    except Exception as e:
        logger.error(f"DB health check failed: {e}")
        return {
            "status": "error",
            "db_path": str(_db_path),
            "error": str(e),
        }


# ─────────────────────────────────────────────
# Utility: row to dict
# ─────────────────────────────────────────────
def row_to_dict(row: Optional[sqlite3.Row]) -> Optional[dict]:
    """Konversi sqlite3.Row ke dict biasa. Return None jika row=None."""
    if row is None:
        return None
    return dict(row)


def rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict]:
    """Konversi list sqlite3.Row ke list dict."""
    return [dict(r) for r in rows]
