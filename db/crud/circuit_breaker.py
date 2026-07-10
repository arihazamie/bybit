"""
db/crud/circuit_breaker.py
CRUD functions untuk tabel `circuit_breaker_state`.

Implementasi state machine per komponen:
    CLOSED  → normal, tidak ada error
    OPEN    → trip, eksekusi sinyal berhenti
    HALF_OPEN → setelah /resume, coba 1 operasi test

Alur transisi:
    CLOSED  -- N critical error dalam T menit --> OPEN
    OPEN    -- /resume dipanggil user        --> HALF_OPEN
    HALF_OPEN -- operasi test berhasil       --> CLOSED
    HALF_OPEN -- operasi test gagal          --> OPEN

Semua fungsi tersedia dalam versi sync dan async.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from db.database import get_db, row_to_dict, rows_to_dicts
from db.models import DEFAULT_CIRCUIT_BREAKER_COMPONENTS

logger = logging.getLogger(__name__)

# Konstanta state
STATE_CLOSED    = "closed"
STATE_OPEN      = "open"
STATE_HALF_OPEN = "half_open"


# ─────────────────────────────────────────────
# Helper
# ─────────────────────────────────────────────

def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validate_component(component: str) -> None:
    if component not in DEFAULT_CIRCUIT_BREAKER_COMPONENTS:
        raise ValueError(
            f"Unknown component: '{component}'. "
            f"Valid: {DEFAULT_CIRCUIT_BREAKER_COMPONENTS}"
        )


# ─────────────────────────────────────────────
# Sync CRUD — Read
# ─────────────────────────────────────────────

def get_cb_state(component: str) -> Optional[dict]:
    """
    Ambil state circuit breaker untuk satu komponen.
    Return dict dengan semua field, atau None jika komponen belum ter-seed.
    """
    _validate_component(component)
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM circuit_breaker_state WHERE component = ?",
            (component,),
        ).fetchone()
    return row_to_dict(row)


def get_all_cb_states() -> list[dict]:
    """
    Ambil state semua komponen.
    Dipakai oleh /status command Telegram untuk menampilkan kesehatan tiap komponen.
    """
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM circuit_breaker_state ORDER BY component"
        ).fetchall()
    return rows_to_dicts(rows)


def is_cb_open(component: str) -> bool:
    """
    Cek apakah circuit breaker komponen ini sedang OPEN (trip).
    Return True jika OPEN atau HALF_OPEN (belum fully recovered).
    """
    state = get_cb_state(component)
    if state is None:
        return False
    return state["state"] in (STATE_OPEN, STATE_HALF_OPEN)


def is_any_cb_open() -> bool:
    """
    Cek apakah ada komponen manapun yang circuit breaker-nya OPEN.
    Dipakai sebelum eksekusi sinyal baru — kalau ada yang open, tolak eksekusi.
    """
    with get_db() as conn:
        row = conn.execute(
            """SELECT COUNT(*) as cnt FROM circuit_breaker_state
               WHERE state IN (?, ?)""",
            (STATE_OPEN, STATE_HALF_OPEN),
        ).fetchone()
    return row["cnt"] > 0 if row else False


def get_open_components() -> list[str]:
    """Return list nama komponen yang sedang OPEN atau HALF_OPEN."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT component FROM circuit_breaker_state
               WHERE state IN (?, ?)""",
            (STATE_OPEN, STATE_HALF_OPEN),
        ).fetchall()
    return [r["component"] for r in rows]


# ─────────────────────────────────────────────
# Sync CRUD — State transitions
# ─────────────────────────────────────────────

def record_error(
    component: str,
    error_message: str,
) -> dict:
    """
    Catat satu critical error untuk komponen.
    Increment consecutive_error_count, update last_error_message dan last_error_at.

    Return: state terbaru komponen (dict) setelah update.

    Caller (circuit breaker logic) bertanggung jawab untuk:
    - Membaca consecutive_error_count dari return value
    - Membandingkan dengan threshold dari settings
    - Memanggil trip_circuit_breaker() jika threshold tercapai
    """
    _validate_component(component)
    now = _utcnow()

    with get_db() as conn:
        conn.execute(
            """UPDATE circuit_breaker_state
               SET consecutive_error_count = consecutive_error_count + 1,
                   last_error_message = ?,
                   last_error_at = ?
               WHERE component = ?""",
            (error_message, now, component),
        )
        row = conn.execute(
            "SELECT * FROM circuit_breaker_state WHERE component = ?",
            (component,),
        ).fetchone()

    state = row_to_dict(row) or {}
    logger.warning(
        f"CB error recorded [{component}]: count={state.get('consecutive_error_count')}, "
        f"msg={error_message[:100]}"
    )
    return state


def trip_circuit_breaker(
    component: str,
    last_error_message: Optional[str] = None,
) -> bool:
    """
    Trip circuit breaker: state → OPEN.
    Set opened_at ke sekarang.

    Dipanggil setelah consecutive_error_count mencapai threshold.
    Return True jika berhasil.
    """
    _validate_component(component)
    now = _utcnow()

    with get_db() as conn:
        cur = conn.execute(
            """UPDATE circuit_breaker_state
               SET state = ?,
                   opened_at = ?,
                   last_error_message = COALESCE(?, last_error_message),
                   last_error_at = ?
               WHERE component = ?""",
            (STATE_OPEN, now, last_error_message, now, component),
        )
        updated = cur.rowcount > 0

    if updated:
        logger.critical(
            f"🚨 CIRCUIT BREAKER TRIPPED: [{component}] → OPEN at {now}. "
            f"Error: {last_error_message}"
        )
    return updated


def transition_to_half_open(component: str) -> bool:
    """
    Transisi dari OPEN → HALF_OPEN setelah user panggil /resume.
    Hanya berpengaruh jika state saat ini OPEN.
    Return True jika berhasil transisi.
    """
    _validate_component(component)

    with get_db() as conn:
        cur = conn.execute(
            """UPDATE circuit_breaker_state
               SET state = ?
               WHERE component = ? AND state = ?""",
            (STATE_HALF_OPEN, component, STATE_OPEN),
        )
        updated = cur.rowcount > 0

    if updated:
        logger.info(f"CB [{component}]: OPEN → HALF_OPEN (test probe akan dilakukan)")
    else:
        logger.debug(f"CB [{component}]: transition_to_half_open skip (state bukan OPEN)")
    return updated


def reset_circuit_breaker(component: str) -> bool:
    """
    Reset circuit breaker ke CLOSED dan bersihkan error counter.
    Dipanggil setelah operasi test berhasil (HALF_OPEN → CLOSED).
    Return True jika berhasil.
    """
    _validate_component(component)

    with get_db() as conn:
        cur = conn.execute(
            """UPDATE circuit_breaker_state
               SET state = ?,
                   consecutive_error_count = 0,
                   opened_at = NULL
               WHERE component = ?""",
            (STATE_CLOSED, component),
        )
        updated = cur.rowcount > 0

    if updated:
        logger.info(f"✅ CB [{component}]: reset → CLOSED, error counter direset")
    return updated


def reset_error_count(component: str) -> bool:
    """
    Reset consecutive_error_count saja (setelah operasi berhasil di state CLOSED).
    Tidak mengubah state.
    """
    _validate_component(component)

    with get_db() as conn:
        cur = conn.execute(
            """UPDATE circuit_breaker_state
               SET consecutive_error_count = 0
               WHERE component = ?""",
            (component,),
        )
        updated = cur.rowcount > 0

    if updated:
        logger.debug(f"CB [{component}]: error counter direset ke 0")
    return updated


def resume_all_components() -> list[str]:
    """
    Transisi semua komponen yang OPEN → HALF_OPEN.
    Dipanggil saat user jalankan /resume tanpa specify komponen tertentu.
    Return list komponen yang berhasil ditransisikan.
    """
    transitioned = []
    for component in DEFAULT_CIRCUIT_BREAKER_COMPONENTS:
        if transition_to_half_open(component):
            transitioned.append(component)

    if transitioned:
        logger.info(f"CB resume: {transitioned} → HALF_OPEN")
    else:
        logger.debug("CB resume: tidak ada komponen yang perlu di-resume")
    return transitioned


def get_cb_summary_for_dashboard() -> dict:
    """
    Ringkasan state circuit breaker untuk /status command.

    Return:
    - overall_healthy: True jika semua CLOSED
    - components: dict {component_name: {state, error_count, last_error, opened_at}}
    - tripped_count: jumlah komponen yang OPEN/HALF_OPEN
    """
    states = get_all_cb_states()

    components_summary = {}
    tripped = []

    for s in states:
        comp = s["component"]
        components_summary[comp] = {
            "state": s["state"],
            "error_count": s["consecutive_error_count"],
            "last_error": s.get("last_error_message"),
            "last_error_at": s.get("last_error_at"),
            "opened_at": s.get("opened_at"),
        }
        if s["state"] in (STATE_OPEN, STATE_HALF_OPEN):
            tripped.append(comp)

    return {
        "overall_healthy": len(tripped) == 0,
        "components": components_summary,
        "tripped_count": len(tripped),
        "tripped_components": tripped,
    }


# ─────────────────────────────────────────────
# Async wrappers
# ─────────────────────────────────────────────

def _run_in_executor(fn, *args, **kwargs):
    loop = asyncio.get_event_loop()
    return loop.run_in_executor(None, lambda: fn(*args, **kwargs))


async def async_get_cb_state(component: str) -> Optional[dict]:
    return await _run_in_executor(get_cb_state, component)


async def async_get_all_cb_states() -> list[dict]:
    return await _run_in_executor(get_all_cb_states)


async def async_is_cb_open(component: str) -> bool:
    return await _run_in_executor(is_cb_open, component)


async def async_is_any_cb_open() -> bool:
    return await _run_in_executor(is_any_cb_open)


async def async_record_error(component: str, error_message: str) -> dict:
    return await _run_in_executor(record_error, component, error_message)


async def async_trip_circuit_breaker(
    component: str,
    last_error_message: Optional[str] = None,
) -> bool:
    return await _run_in_executor(trip_circuit_breaker, component, last_error_message)


async def async_transition_to_half_open(component: str) -> bool:
    return await _run_in_executor(transition_to_half_open, component)


async def async_reset_circuit_breaker(component: str) -> bool:
    return await _run_in_executor(reset_circuit_breaker, component)


async def async_reset_error_count(component: str) -> bool:
    return await _run_in_executor(reset_error_count, component)


async def async_resume_all_components() -> list[str]:
    return await _run_in_executor(resume_all_components)


async def async_get_cb_summary_for_dashboard() -> dict:
    return await _run_in_executor(get_cb_summary_for_dashboard)
