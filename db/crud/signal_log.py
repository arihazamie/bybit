"""
db/crud/signal_log.py
CRUD functions untuk tabel `signal_log`.

Tabel ini menyimpan semua pesan dari Telegram grup sinyal.
Fungsi utama:
- Idempotency check via message_id (UNIQUE constraint)
- Log hasil parsing
- Update action_taken dan trade_id setelah eksekusi
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from db.database import get_db, row_to_dict, rows_to_dicts

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# Sync CRUD
# ─────────────────────────────────────────────

def is_message_processed(message_id: int, chat_id: int = 0) -> bool:
    """
    IDEMPOTENCY CHECK: apakah pesan dengan (chat_id, message_id) ini sudah
    pernah diproses? Return True jika sudah ada di DB, False jika belum.

    message_id Telegram cuma unik PER-CHAT, bukan global — kalau listen
    lebih dari satu grup, message_id BISA bentrok antar chat_id berbeda.
    Makanya cek harus pasangan (chat_id, message_id), bukan message_id saja,
    supaya sinyal valid dari chat lain tidak salah ke-skip.

    Fungsi ini WAJIB dipanggil sebelum memproses sinyal apapun.
    Jika return True → skip, jangan proses ulang.
    """
    with get_db() as conn:
        row = conn.execute(
            "SELECT id FROM signal_log WHERE chat_id = ? AND message_id = ?",
            (chat_id, message_id),
        ).fetchone()
        return row is not None


def create_signal_log(
    *,
    message_id: int,
    chat_id: Optional[int],
    sender_username: Optional[str],
    raw_text: str,
    received_at: str,                   # ISO8601 UTC string
    parsed_status: str,                 # 'success'|'ambiguous'|'info_only'|'error'
    parsed_data: Optional[dict] = None,
    ambiguity_reasons: Optional[list] = None,
    action_taken: Optional[str] = None,
    trade_id: Optional[int] = None,
) -> Optional[int]:
    """
    Insert satu record ke signal_log.

    Return: id record yang baru dibuat, atau None jika message_id sudah ada
            (idempotency — tidak raise exception, hanya return None).
    """
    parsed_data_json = json.dumps(parsed_data) if parsed_data else None
    ambiguity_json = json.dumps(ambiguity_reasons) if ambiguity_reasons else None

    try:
        with get_db() as conn:
            cur = conn.execute(
                """INSERT OR IGNORE INTO signal_log
                   (message_id, chat_id, sender_username, raw_text, received_at,
                    parsed_status, parsed_data, ambiguity_reasons, action_taken, trade_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    message_id,
                    chat_id,
                    sender_username,
                    raw_text,
                    received_at,
                    parsed_status,
                    parsed_data_json,
                    ambiguity_json,
                    action_taken,
                    trade_id,
                ),
            )

            if cur.rowcount == 0:
                # INSERT OR IGNORE tidak insert karena sudah ada (idempotency)
                logger.debug(f"Signal log skipped (already exists): message_id={message_id}")
                return None

            new_id = cur.lastrowid
            logger.debug(f"Signal log created: id={new_id}, message_id={message_id}, status={parsed_status}")
            return new_id

    except Exception as e:
        logger.error(f"Failed to create signal log for message_id={message_id}: {e}")
        raise


def update_signal_action(
    signal_log_id: int,
    *,
    action_taken: str,
    trade_id: Optional[int] = None,
) -> bool:
    """
    Update action_taken dan trade_id setelah bot memutuskan apa yang dilakukan.
    Return True jika berhasil, False jika record tidak ditemukan.
    """
    with get_db() as conn:
        cur = conn.execute(
            """UPDATE signal_log
               SET action_taken = ?,
                   trade_id = COALESCE(?, trade_id)
               WHERE id = ?""",
            (action_taken, trade_id, signal_log_id),
        )
        success = cur.rowcount > 0

    if success:
        logger.debug(f"Signal log updated: id={signal_log_id}, action={action_taken}")
    else:
        logger.warning(f"Signal log not found for update: id={signal_log_id}")

    return success


def get_signal_log_by_message_id(message_id: int, chat_id: int = 0) -> Optional[dict]:
    """Ambil satu record signal_log berdasarkan (chat_id, Telegram message_id)."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM signal_log WHERE chat_id = ? AND message_id = ?",
            (chat_id, message_id),
        ).fetchone()
        result = row_to_dict(row)

    if result:
        # Deserialize JSON fields
        if result.get("parsed_data"):
            try:
                result["parsed_data"] = json.loads(result["parsed_data"])
            except json.JSONDecodeError:
                pass
        if result.get("ambiguity_reasons"):
            try:
                result["ambiguity_reasons"] = json.loads(result["ambiguity_reasons"])
            except json.JSONDecodeError:
                pass

    return result


def get_signal_log_by_id(log_id: int) -> Optional[dict]:
    """Ambil satu record signal_log berdasarkan id internal."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM signal_log WHERE id = ?", (log_id,)
        ).fetchone()
        return _deserialize_signal_log(row_to_dict(row))


def get_recent_signal_logs(limit: int = 20, status_filter: Optional[str] = None) -> list[dict]:
    """
    Ambil N signal log terbaru, dengan optional filter berdasarkan parsed_status.
    status_filter: 'success'|'ambiguous'|'info_only'|'error'|None (semua)
    """
    with get_db() as conn:
        if status_filter:
            rows = conn.execute(
                "SELECT * FROM signal_log WHERE parsed_status = ? ORDER BY created_at DESC LIMIT ?",
                (status_filter, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM signal_log ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()

    return [_deserialize_signal_log(row_to_dict(r)) for r in rows]


def get_signal_logs_awaiting_confirmation() -> list[dict]:
    """Ambil semua sinyal yang sedang menunggu konfirmasi user."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT * FROM signal_log
               WHERE action_taken = 'awaiting_confirmation'
               ORDER BY created_at DESC""",
        ).fetchall()

    return [_deserialize_signal_log(row_to_dict(r)) for r in rows]


def count_signals_by_status(
    since_iso: Optional[str] = None,
) -> dict[str, int]:
    """
    Hitung jumlah sinyal per status.
    since_iso: filter dari waktu tertentu (ISO8601 UTC string), None = semua waktu.

    Return: {'success': N, 'ambiguous': N, 'info_only': N, 'error': N}
    """
    with get_db() as conn:
        if since_iso:
            rows = conn.execute(
                """SELECT parsed_status, COUNT(*) as cnt
                   FROM signal_log
                   WHERE created_at >= ?
                   GROUP BY parsed_status""",
                (since_iso,),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT parsed_status, COUNT(*) as cnt
                   FROM signal_log
                   GROUP BY parsed_status""",
            ).fetchall()

    counts = {"success": 0, "ambiguous": 0, "info_only": 0, "error": 0}
    for row in rows:
        status = row["parsed_status"] if isinstance(row, dict) else row[0]
        cnt = row["cnt"] if isinstance(row, dict) else row[1]
        if status in counts:
            counts[status] = cnt

    return counts


# ─────────────────────────────────────────────
# Helper internal
# ─────────────────────────────────────────────

def _deserialize_signal_log(record: Optional[dict]) -> Optional[dict]:
    """Deserialize JSON fields dalam record signal_log."""
    if record is None:
        return None

    if record.get("parsed_data") and isinstance(record["parsed_data"], str):
        try:
            record["parsed_data"] = json.loads(record["parsed_data"])
        except json.JSONDecodeError:
            pass

    if record.get("ambiguity_reasons") and isinstance(record["ambiguity_reasons"], str):
        try:
            record["ambiguity_reasons"] = json.loads(record["ambiguity_reasons"])
        except json.JSONDecodeError:
            pass

    return record


# ─────────────────────────────────────────────
# Async versions
# ─────────────────────────────────────────────

async def async_is_message_processed(message_id: int, chat_id: int = 0) -> bool:
    """Async version of is_message_processed."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, is_message_processed, message_id, chat_id)


async def async_create_signal_log(**kwargs) -> Optional[int]:
    """Async version of create_signal_log."""
    loop = asyncio.get_event_loop()

    def _run():
        return create_signal_log(**kwargs)

    return await loop.run_in_executor(None, _run)


async def async_update_signal_action(
    signal_log_id: int,
    *,
    action_taken: str,
    trade_id: Optional[int] = None,
) -> bool:
    """Async version of update_signal_action."""
    loop = asyncio.get_event_loop()

    def _run():
        return update_signal_action(signal_log_id, action_taken=action_taken, trade_id=trade_id)

    return await loop.run_in_executor(None, _run)


async def async_get_signal_log_by_message_id(message_id: int, chat_id: int = 0) -> Optional[dict]:
    """Async version of get_signal_log_by_message_id."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, get_signal_log_by_message_id, message_id, chat_id)


async def async_get_signal_logs_awaiting_confirmation() -> list[dict]:
    """Async version of get_signal_logs_awaiting_confirmation."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, get_signal_logs_awaiting_confirmation)