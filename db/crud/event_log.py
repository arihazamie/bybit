"""
db/crud/event_log.py
CRUD functions untuk tabel `event_log`.

Audit trail untuk semua kejadian penting:
- Circuit breaker trip/reset
- Leverage auto-adjusted
- Position conflict
- Liquidation warning
- SL hit, TP hit, entry fill
- Order gagal
- Bot pause/resume
- Settings changed

Fungsi utama:
- log_event() — insert satu event baru (fire-and-forget, tidak raise)
- Query helpers: recent events, by severity, by type, per trade

Catatan: log_event TIDAK boleh raise exception — logging failure tidak boleh
mengganggu alur utama bot. Semua error dicatch dan ditulis ke Python logger saja.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from db.database import get_db, row_to_dict, rows_to_dicts

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# Valid values (sesuai schema models.py)
# ─────────────────────────────────────────────
VALID_EVENT_TYPES = {
    "circuit_breaker_trip",
    "circuit_breaker_reset",
    "leverage_adjusted",
    "position_conflict",
    "liquidation_warning",
    "sl_hit",
    "tp_hit",
    "entry_filled",
    "order_failed",
    "bot_paused",
    "bot_resumed",
    "settings_changed",
    "other",
}

VALID_SEVERITIES = {"info", "warning", "critical"}


# ─────────────────────────────────────────────
# Helper
# ─────────────────────────────────────────────

def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─────────────────────────────────────────────
# Sync CRUD — Write
# ─────────────────────────────────────────────

def log_event(
    event_type: str,
    message: str,
    *,
    component: Optional[str] = None,
    severity: str = "info",
    trade_id: Optional[int] = None,
) -> Optional[int]:
    """
    Insert satu event ke event_log.

    Desain: fire-and-forget — tidak raise exception.
    Jika insert gagal (DB error, tipe tidak valid), error ditulis ke Python logger
    dan fungsi return None. Alur utama bot TIDAK terganggu.

    Return: id event yang baru dibuat, atau None jika gagal.
    """
    # Soft-validate (jangan crash, cukup fallback ke 'other'/'info')
    if event_type not in VALID_EVENT_TYPES:
        logger.warning(
            f"log_event: unknown event_type '{event_type}', fallback ke 'other'"
        )
        event_type = "other"

    if severity not in VALID_SEVERITIES:
        logger.warning(
            f"log_event: unknown severity '{severity}', fallback ke 'info'"
        )
        severity = "info"

    try:
        now = _utcnow()
        with get_db() as conn:
            cur = conn.execute(
                """INSERT INTO event_log
                   (event_type, component, message, severity, trade_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (event_type, component, message, severity, trade_id, now),
            )
            event_id = cur.lastrowid

        # Juga log ke Python logger sesuai severity
        log_fn = {
            "info": logger.info,
            "warning": logger.warning,
            "critical": logger.critical,
        }.get(severity, logger.info)

        log_fn(
            f"[EVENT:{severity.upper()}] type={event_type}"
            + (f" component={component}" if component else "")
            + (f" trade_id={trade_id}" if trade_id else "")
            + f" | {message}"
        )

        return event_id

    except Exception as e:
        # TIDAK re-raise — audit trail failure tidak boleh crash bot
        logger.error(f"log_event FAILED (event_type={event_type}): {e}")
        return None


# ─────────────────────────────────────────────
# Convenience wrappers untuk event-event umum
# ─────────────────────────────────────────────

def log_circuit_breaker_trip(component: str, error_message: str) -> Optional[int]:
    return log_event(
        "circuit_breaker_trip",
        f"Circuit breaker tripped on [{component}]: {error_message}",
        component=component,
        severity="critical",
    )


def log_circuit_breaker_reset(component: str) -> Optional[int]:
    return log_event(
        "circuit_breaker_reset",
        f"Circuit breaker [{component}] reset → CLOSED",
        component=component,
        severity="info",
    )


def log_leverage_adjusted(
    pair: str,
    original_leverage: float,
    adjusted_leverage: float,
    reason: str,
    trade_id: Optional[int] = None,
) -> Optional[int]:
    return log_event(
        "leverage_adjusted",
        f"{pair}: leverage {original_leverage}x → {adjusted_leverage}x | reason: {reason}",
        component="order_execution",
        severity="warning",
        trade_id=trade_id,
    )


def log_position_conflict(
    pair: str,
    conflict_detail: str,
    action_taken: str,
    trade_id: Optional[int] = None,
) -> Optional[int]:
    return log_event(
        "position_conflict",
        f"{pair} position conflict: {conflict_detail} | action: {action_taken}",
        component="order_execution",
        severity="warning",
        trade_id=trade_id,
    )


def log_liquidation_warning(
    pair: str,
    detail: str,
    trade_id: Optional[int] = None,
) -> Optional[int]:
    return log_event(
        "liquidation_warning",
        f"{pair}: {detail}",
        component="order_execution",
        severity="critical",
        trade_id=trade_id,
    )


def log_sl_hit(pair: str, sl_price: float, pnl: float, trade_id: int) -> Optional[int]:
    return log_event(
        "sl_hit",
        f"{pair} SL hit at {sl_price} | PnL: {pnl:.4f} USDT",
        component="order_execution",
        severity="warning",
        trade_id=trade_id,
    )


def log_tp_hit(pair: str, tp_price: float, pnl: float, trade_id: int) -> Optional[int]:
    return log_event(
        "tp_hit",
        f"{pair} TP hit at {tp_price} | PnL: +{pnl:.4f} USDT",
        component="order_execution",
        severity="info",
        trade_id=trade_id,
    )


def log_entry_filled(
    pair: str,
    fill_price: float,
    trade_id: int,
) -> Optional[int]:
    return log_event(
        "entry_filled",
        f"{pair} entry filled at {fill_price}",
        component="order_execution",
        severity="info",
        trade_id=trade_id,
    )


def log_order_failed(
    pair: str,
    reason: str,
    trade_id: Optional[int] = None,
) -> Optional[int]:
    return log_event(
        "order_failed",
        f"{pair} order failed: {reason}",
        component="order_execution",
        severity="critical",
        trade_id=trade_id,
    )


def log_bot_paused(reason: str = "manual") -> Optional[int]:
    return log_event(
        "bot_paused",
        f"Bot paused ({reason})",
        severity="warning",
    )


def log_bot_resumed() -> Optional[int]:
    return log_event(
        "bot_resumed",
        "Bot resumed — eksekusi sinyal aktif kembali",
        severity="info",
    )


def log_settings_changed(key: str, old_value: str, new_value: str) -> Optional[int]:
    return log_event(
        "settings_changed",
        f"Setting '{key}' changed: {old_value!r} → {new_value!r}",
        severity="info",
    )


# ─────────────────────────────────────────────
# Sync CRUD — Read / Query
# ─────────────────────────────────────────────

def get_recent_events(limit: int = 50) -> list[dict]:
    """Ambil N event terbaru, diurutkan dari yang terbaru."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM event_log ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return rows_to_dicts(rows)


def get_events_by_severity(severity: str, limit: int = 50) -> list[dict]:
    """
    Ambil event berdasarkan severity (info / warning / critical).
    Berguna untuk /status atau alert filtering.
    """
    with get_db() as conn:
        rows = conn.execute(
            """SELECT * FROM event_log
               WHERE severity = ?
               ORDER BY created_at DESC LIMIT ?""",
            (severity, limit),
        ).fetchall()
    return rows_to_dicts(rows)


def get_events_by_type(event_type: str, limit: int = 50) -> list[dict]:
    """Ambil event berdasarkan event_type."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT * FROM event_log
               WHERE event_type = ?
               ORDER BY created_at DESC LIMIT ?""",
            (event_type, limit),
        ).fetchall()
    return rows_to_dicts(rows)


def get_events_for_trade(trade_id: int) -> list[dict]:
    """Ambil semua event yang terkait dengan satu trade tertentu."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT * FROM event_log
               WHERE trade_id = ?
               ORDER BY created_at ASC""",
            (trade_id,),
        ).fetchall()
    return rows_to_dicts(rows)


def get_critical_events_since(since_iso_utc: str, limit: int = 20) -> list[dict]:
    """
    Ambil event critical sejak waktu tertentu.
    Dipakai untuk alert periodic atau health check.
    """
    with get_db() as conn:
        rows = conn.execute(
            """SELECT * FROM event_log
               WHERE severity = 'critical' AND created_at >= ?
               ORDER BY created_at DESC LIMIT ?""",
            (since_iso_utc, limit),
        ).fetchall()
    return rows_to_dicts(rows)


def get_event_by_id(event_id: int) -> Optional[dict]:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM event_log WHERE id = ?",
            (event_id,),
        ).fetchone()
    return row_to_dict(row)


def count_events_by_type_since(event_type: str, since_iso_utc: str) -> int:
    """
    Hitung jumlah event dengan tipe tertentu sejak waktu tertentu.
    Berguna untuk circuit breaker: menghitung berapa kali error dalam window T.
    """
    with get_db() as conn:
        row = conn.execute(
            """SELECT COUNT(*) as cnt FROM event_log
               WHERE event_type = ? AND created_at >= ?""",
            (event_type, since_iso_utc),
        ).fetchone()
    return row["cnt"] if row else 0


# ─────────────────────────────────────────────
# Async wrappers
# ─────────────────────────────────────────────

def _run_in_executor(fn, *args, **kwargs):
    loop = asyncio.get_event_loop()
    return loop.run_in_executor(None, lambda: fn(*args, **kwargs))


async def async_log_event(
    event_type: str,
    message: str,
    *,
    component: Optional[str] = None,
    severity: str = "info",
    trade_id: Optional[int] = None,
) -> Optional[int]:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None,
        lambda: log_event(
            event_type,
            message,
            component=component,
            severity=severity,
            trade_id=trade_id,
        ),
    )


async def async_log_circuit_breaker_trip(
    component: str, error_message: str
) -> Optional[int]:
    return await _run_in_executor(log_circuit_breaker_trip, component, error_message)


async def async_log_circuit_breaker_reset(component: str) -> Optional[int]:
    return await _run_in_executor(log_circuit_breaker_reset, component)


async def async_log_leverage_adjusted(
    pair: str,
    original_leverage: float,
    adjusted_leverage: float,
    reason: str,
    trade_id: Optional[int] = None,
) -> Optional[int]:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None,
        lambda: log_leverage_adjusted(
            pair, original_leverage, adjusted_leverage, reason, trade_id
        ),
    )


async def async_log_position_conflict(
    pair: str,
    conflict_detail: str,
    action_taken: str,
    trade_id: Optional[int] = None,
) -> Optional[int]:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None,
        lambda: log_position_conflict(pair, conflict_detail, action_taken, trade_id),
    )


async def async_log_liquidation_warning(
    pair: str, detail: str, trade_id: Optional[int] = None
) -> Optional[int]:
    return await _run_in_executor(log_liquidation_warning, pair, detail, trade_id)


async def async_log_sl_hit(
    pair: str, sl_price: float, pnl: float, trade_id: int
) -> Optional[int]:
    return await _run_in_executor(log_sl_hit, pair, sl_price, pnl, trade_id)


async def async_log_tp_hit(
    pair: str, tp_price: float, pnl: float, trade_id: int
) -> Optional[int]:
    return await _run_in_executor(log_tp_hit, pair, tp_price, pnl, trade_id)


async def async_log_entry_filled(
    pair: str, fill_price: float, trade_id: int
) -> Optional[int]:
    return await _run_in_executor(log_entry_filled, pair, fill_price, trade_id)


async def async_log_order_failed(
    pair: str, reason: str, trade_id: Optional[int] = None
) -> Optional[int]:
    return await _run_in_executor(log_order_failed, pair, reason, trade_id)


async def async_log_bot_paused(reason: str = "manual") -> Optional[int]:
    return await _run_in_executor(log_bot_paused, reason)


async def async_log_bot_resumed() -> Optional[int]:
    return await _run_in_executor(log_bot_resumed)


async def async_log_settings_changed(
    key: str, old_value: str, new_value: str
) -> Optional[int]:
    return await _run_in_executor(log_settings_changed, key, old_value, new_value)


async def async_get_recent_events(limit: int = 50) -> list[dict]:
    return await _run_in_executor(get_recent_events, limit)


async def async_get_events_by_severity(severity: str, limit: int = 50) -> list[dict]:
    return await _run_in_executor(get_events_by_severity, severity, limit)


async def async_get_events_for_trade(trade_id: int) -> list[dict]:
    return await _run_in_executor(get_events_for_trade, trade_id)


async def async_get_critical_events_since(
    since_iso_utc: str, limit: int = 20
) -> list[dict]:
    return await _run_in_executor(get_critical_events_since, since_iso_utc, limit)
