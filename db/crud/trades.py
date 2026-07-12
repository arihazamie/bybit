"""
db/crud/trades.py
CRUD functions untuk tabel `trades`.

Mencakup:
- Buat trade baru (insert)
- Ambil trade by ID atau filter
- Update status, SL, TP, entry, leverage
- Close trade (isi closed_at + PnL + close_reason)
- Query helpers: list open trades, closed trades, stats harian
- Async wrappers via thread pool executor
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from db.database import get_db, row_to_dict, rows_to_dicts

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# Helper: timestamp UTC
# ─────────────────────────────────────────────

def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─────────────────────────────────────────────
# Sync CRUD
# ─────────────────────────────────────────────

def create_trade(
    *,
    pair: str,
    direction: str,                         # 'long' | 'short'
    entry_type: str,                        # 'limit' | 'market'
    entry_price: float,
    sl_price: float,
    tp_price: Optional[float] = None,
    position_size: float,
    margin_used: Optional[float] = None,
    risk_mode: str,                         # 'percent' | 'fixed_usd'
    risk_amount_usd: float,
    risk_percent_used: Optional[float] = None,
    max_leverage_available: Optional[float] = None,
    leverage_used: Optional[float] = None,
    leverage_auto_adjusted: bool = False,
    liquidation_price_estimate: Optional[float] = None,
    status: str = "pending",               # 'pending'|'open'|'closed'|'cancelled'
    opened_at: Optional[str] = None,
    raw_signal_text: Optional[str] = None,
    source_analyst: Optional[str] = None,
    source_message_id: Optional[int] = None,
    conflict_action_taken: Optional[str] = None,
) -> int:
    """
    Insert trade baru ke tabel trades.
    Return: id trade yang baru dibuat.
    """
    now = _utcnow()
    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO trades (
                pair, direction, entry_type, entry_price, sl_price, tp_price,
                position_size, margin_used,
                risk_mode, risk_amount_usd, risk_percent_used,
                max_leverage_available, leverage_used, leverage_auto_adjusted,
                liquidation_price_estimate,
                status, opened_at,
                raw_signal_text, source_analyst, source_message_id,
                conflict_action_taken,
                created_at, updated_at
            ) VALUES (
                ?, ?, ?, ?, ?, ?,
                ?, ?,
                ?, ?, ?,
                ?, ?, ?,
                ?,
                ?, ?,
                ?, ?, ?,
                ?,
                ?, ?
            )
            """,
            (
                pair, direction, entry_type, entry_price, sl_price, tp_price,
                position_size, margin_used,
                risk_mode, risk_amount_usd, risk_percent_used,
                max_leverage_available, leverage_used, int(leverage_auto_adjusted),
                liquidation_price_estimate,
                status, opened_at,
                raw_signal_text, source_analyst, source_message_id,
                conflict_action_taken,
                now, now,
            ),
        )
        trade_id = cur.lastrowid

    logger.info(f"Trade created: id={trade_id}, pair={pair}, dir={direction}, status={status}")
    return trade_id


def get_trade_by_id(trade_id: int) -> Optional[dict]:
    """Ambil satu trade berdasarkan id. Return None jika tidak ada."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM trades WHERE id = ?", (trade_id,)
        ).fetchone()
    return row_to_dict(row)


def get_trade_by_pair_and_status(
    pair: str,
    status: str,
) -> Optional[dict]:
    """
    Ambil satu trade berdasarkan pair dan status.
    Berguna untuk cek apakah ada posisi open / pending untuk pair tertentu.
    Return trade terbaru jika ada lebih dari satu (ordered by created_at DESC).
    """
    with get_db() as conn:
        row = conn.execute(
            """SELECT * FROM trades
               WHERE pair = ? AND status = ?
               ORDER BY created_at DESC LIMIT 1""",
            (pair, status),
        ).fetchone()
    return row_to_dict(row)


def get_open_trades() -> list[dict]:
    """
    Ambil semua trade dengan status 'open' atau 'pending'.
    Dipakai oleh monitoring loop untuk tracking posisi aktif.
    """
    with get_db() as conn:
        rows = conn.execute(
            """SELECT * FROM trades
               WHERE status IN ('open', 'pending')
               ORDER BY created_at ASC""",
        ).fetchall()
    return rows_to_dicts(rows)


def get_open_trade_for_pair(pair: str) -> Optional[dict]:
    """
    Cek apakah ada posisi open/pending untuk pair tertentu.
    Return trade terbaru, atau None jika tidak ada.
    """
    with get_db() as conn:
        row = conn.execute(
            """SELECT * FROM trades
               WHERE pair = ? AND status IN ('open', 'pending')
               ORDER BY created_at DESC LIMIT 1""",
            (pair,),
        ).fetchone()
    return row_to_dict(row)


def get_closed_trades(limit: int = 50) -> list[dict]:
    """
    Ambil trade yang sudah ditutup, terbaru dulu.
    Dipakai untuk /history command.
    """
    with get_db() as conn:
        rows = conn.execute(
            """SELECT * FROM trades
               WHERE status = 'closed'
               ORDER BY closed_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return rows_to_dicts(rows)


def get_all_trades_by_pair(pair: str) -> list[dict]:
    """Ambil semua trade untuk pair tertentu, terbaru dulu."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM trades WHERE pair = ? ORDER BY created_at DESC",
            (pair,),
        ).fetchall()
    return rows_to_dicts(rows)


# ─────────────────────────────────────────────
# Update helpers
# ─────────────────────────────────────────────

def update_trade_status(
    trade_id: int,
    status: str,
    opened_at: Optional[str] = None,
) -> bool:
    """
    Update status trade (pending → open → closed / cancelled).
    Jika status = 'open' dan opened_at tidak di-pass, isi otomatis dengan waktu sekarang.
    Return True jika row berhasil diupdate.
    """
    if status == "open" and opened_at is None:
        opened_at = _utcnow()

    with get_db() as conn:
        cur = conn.execute(
            """UPDATE trades
               SET status = ?,
                   opened_at = COALESCE(?, opened_at)
               WHERE id = ?""",
            (status, opened_at, trade_id),
        )
        updated = cur.rowcount > 0

    if updated:
        logger.info(f"Trade {trade_id} status → {status}")
    else:
        logger.warning(f"update_trade_status: trade {trade_id} not found")
    return updated


def close_trade(
    trade_id: int,
    *,
    close_reason: str,              # 'sl_hit'|'tp_hit'|'manual_close'|'liquidated'
    pnl: Optional[float] = None,
    r_multiple: Optional[float] = None,
    closed_at: Optional[str] = None,
) -> bool:
    """
    Tandai trade sebagai closed, isi PnL, R-multiple, close_reason, dan waktu tutup.
    Return True jika berhasil.
    """
    if closed_at is None:
        closed_at = _utcnow()

    with get_db() as conn:
        cur = conn.execute(
            """UPDATE trades
               SET status = 'closed',
                   close_reason = ?,
                   pnl = ?,
                   r_multiple = ?,
                   closed_at = ?
               WHERE id = ?""",
            (close_reason, pnl, r_multiple, closed_at, trade_id),
        )
        updated = cur.rowcount > 0

    if updated:
        logger.info(
            f"Trade {trade_id} closed: reason={close_reason}, pnl={pnl}, R={r_multiple}"
        )
    else:
        logger.warning(f"close_trade: trade {trade_id} not found")
    return updated


def cancel_trade(trade_id: int) -> bool:
    """Tandai trade sebagai cancelled (limit order tidak pernah fill)."""
    with get_db() as conn:
        cur = conn.execute(
            "UPDATE trades SET status = 'cancelled' WHERE id = ?",
            (trade_id,),
        )
        updated = cur.rowcount > 0

    if updated:
        logger.info(f"Trade {trade_id} cancelled")
    return updated


def update_trade_sl(
    trade_id: int,
    new_sl_price: float,
    sl_order_id: Optional[str] = None,
) -> bool:
    """
    Update Stop Loss harga untuk trade yang sedang open.

    sl_order_id: opsional — id order SL yang baru dipasang di exchange.
    Disimpan supaya panggilan /setsl berikutnya bisa cancel order lama
    sebelum memasang yang baru (mencegah order SL menumpuk di exchange).
    Jika None, kolom sl_order_id tidak diubah (dipertahankan apa adanya).
    """
    with get_db() as conn:
        if sl_order_id is not None:
            cur = conn.execute(
                "UPDATE trades SET sl_price = ?, sl_order_id = ? "
                "WHERE id = ? AND status = 'open'",
                (new_sl_price, sl_order_id, trade_id),
            )
        else:
            cur = conn.execute(
                "UPDATE trades SET sl_price = ? WHERE id = ? AND status = 'open'",
                (new_sl_price, trade_id),
            )
        updated = cur.rowcount > 0

    if updated:
        logger.info(f"Trade {trade_id} SL updated → {new_sl_price}")
    else:
        logger.warning(f"update_trade_sl: trade {trade_id} not found or not open")
    return updated


def update_trade_tp(trade_id: int, new_tp_price: float) -> bool:
    """Set atau update Take Profit untuk trade yang sedang open/pending."""
    with get_db() as conn:
        cur = conn.execute(
            """UPDATE trades SET tp_price = ?
               WHERE id = ? AND status IN ('open', 'pending')""",
            (new_tp_price, trade_id),
        )
        updated = cur.rowcount > 0

    if updated:
        logger.info(f"Trade {trade_id} TP set → {new_tp_price}")
    else:
        logger.warning(f"update_trade_tp: trade {trade_id} not found or not open/pending")
    return updated


def update_trade_entry(trade_id: int, new_entry_price: float) -> bool:
    """Update harga entry untuk trade yang masih pending (limit order belum fill)."""
    with get_db() as conn:
        cur = conn.execute(
            "UPDATE trades SET entry_price = ? WHERE id = ? AND status = 'pending'",
            (new_entry_price, trade_id),
        )
        updated = cur.rowcount > 0

    if updated:
        logger.info(f"Trade {trade_id} entry updated → {new_entry_price}")
    else:
        logger.warning(f"update_trade_entry: trade {trade_id} not found or not pending")
    return updated


def update_trade_margin(
    trade_id: int,
    *,
    margin_used: float,
    leverage_used: Optional[float] = None,
    leverage_auto_adjusted: Optional[bool] = None,
    liquidation_price_estimate: Optional[float] = None,
) -> bool:
    """
    Update info margin dan leverage setelah order fill dikonfirmasi.
    Dipanggil oleh executor setelah entry fill terdeteksi via websocket.
    """
    with get_db() as conn:
        cur = conn.execute(
            """UPDATE trades
               SET margin_used = ?,
                   leverage_used = COALESCE(?, leverage_used),
                   leverage_auto_adjusted = COALESCE(?, leverage_auto_adjusted),
                   liquidation_price_estimate = COALESCE(?, liquidation_price_estimate)
               WHERE id = ?""",
            (
                margin_used,
                leverage_used,
                int(leverage_auto_adjusted) if leverage_auto_adjusted is not None else None,
                liquidation_price_estimate,
                trade_id,
            ),
        )
        updated = cur.rowcount > 0

    if updated:
        logger.debug(f"Trade {trade_id} margin info updated")
    return updated


def update_trade_fields(trade_id: int, **fields: Any) -> bool:
    """
    Generic update untuk field-field trade.
    Hanya field yang ada di parameter yang di-update.

    Contoh: update_trade_fields(42, conflict_action_taken='replace', source_analyst='Faith')

    PERHATIAN: tidak ada validasi kolom — hanya pakai untuk field yang diketahui.
    """
    if not fields:
        return False

    set_clauses = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [trade_id]

    with get_db() as conn:
        cur = conn.execute(
            f"UPDATE trades SET {set_clauses} WHERE id = ?",
            values,
        )
        updated = cur.rowcount > 0

    if updated:
        logger.debug(f"Trade {trade_id} fields updated: {list(fields.keys())}")
    return updated


# ─────────────────────────────────────────────
# Stats & reporting queries
# ─────────────────────────────────────────────

def get_daily_stats(date_utc: Optional[str] = None) -> dict:
    """
    Statistik trade untuk satu hari (UTC).
    Jika date_utc tidak di-pass, pakai hari ini.

    Return dict dengan:
    - date: tanggal (YYYY-MM-DD)
    - total_trades: jumlah trade yang ditutup hari ini
    - winning_trades: trade dengan PnL > 0
    - losing_trades: trade dengan PnL <= 0
    - total_pnl: total PnL hari ini
    - avg_r_multiple: rata-rata R-multiple
    """
    if date_utc is None:
        date_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    with get_db() as conn:
        row = conn.execute(
            """SELECT
                COUNT(*) as total_trades,
                SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as winning_trades,
                SUM(CASE WHEN pnl <= 0 THEN 1 ELSE 0 END) as losing_trades,
                COALESCE(SUM(pnl), 0.0) as total_pnl,
                AVG(r_multiple) as avg_r_multiple
               FROM trades
               WHERE status = 'closed'
               AND substr(closed_at, 1, 10) = ?""",
            (date_utc,),
        ).fetchone()

    result = dict(row) if row else {}
    result["date"] = date_utc
    return result


def get_open_trades_summary() -> dict:
    """
    Ringkasan posisi open untuk /dashboard command.

    Return:
    - open_count: jumlah posisi open
    - pending_count: jumlah pending order
    - total_margin_used: estimasi total margin terkunci
    - pairs_open: list pair yang sedang open
    """
    with get_db() as conn:
        row = conn.execute(
            """SELECT
                SUM(CASE WHEN status = 'open' THEN 1 ELSE 0 END) as open_count,
                SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) as pending_count,
                COALESCE(SUM(CASE WHEN status = 'open' THEN margin_used ELSE 0 END), 0) as total_margin_used
               FROM trades
               WHERE status IN ('open', 'pending')""",
        ).fetchone()

        pairs = conn.execute(
            """SELECT pair FROM trades
               WHERE status = 'open'
               ORDER BY opened_at ASC""",
        ).fetchall()

    summary = dict(row) if row else {
        "open_count": 0,
        "pending_count": 0,
        "total_margin_used": 0.0,
    }
    summary["pairs_open"] = [r["pair"] for r in pairs]
    return summary


def count_open_trades() -> int:
    """Jumlah trade open + pending saat ini."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM trades WHERE status IN ('open', 'pending')"
        ).fetchone()
    return row["cnt"] if row else 0


# ─────────────────────────────────────────────
# Async wrappers
# ─────────────────────────────────────────────

def _run_in_executor(fn, *args, **kwargs):
    """Helper: jalankan sync function di thread pool executor."""
    loop = asyncio.get_event_loop()
    return loop.run_in_executor(None, lambda: fn(*args, **kwargs))


async def async_create_trade(**kwargs) -> int:
    return await _run_in_executor(create_trade, **kwargs)


async def async_get_trade_by_id(trade_id: int) -> Optional[dict]:
    return await _run_in_executor(get_trade_by_id, trade_id)


async def async_get_open_trades() -> list[dict]:
    return await _run_in_executor(get_open_trades)


async def async_get_open_trade_for_pair(pair: str) -> Optional[dict]:
    return await _run_in_executor(get_open_trade_for_pair, pair)


async def async_get_closed_trades(limit: int = 50) -> list[dict]:
    return await _run_in_executor(get_closed_trades, limit)


async def async_update_trade_status(
    trade_id: int,
    status: str,
    opened_at: Optional[str] = None,
) -> bool:
    return await _run_in_executor(update_trade_status, trade_id, status, opened_at)


async def async_close_trade(
    trade_id: int,
    *,
    close_reason: str,
    pnl: Optional[float] = None,
    r_multiple: Optional[float] = None,
    closed_at: Optional[str] = None,
) -> bool:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None,
        lambda: close_trade(
            trade_id,
            close_reason=close_reason,
            pnl=pnl,
            r_multiple=r_multiple,
            closed_at=closed_at,
        ),
    )


async def async_update_trade_sl(
    trade_id: int,
    new_sl_price: float,
    sl_order_id: Optional[str] = None,
) -> bool:
    return await _run_in_executor(update_trade_sl, trade_id, new_sl_price, sl_order_id)


async def async_update_trade_tp(trade_id: int, new_tp_price: float) -> bool:
    return await _run_in_executor(update_trade_tp, trade_id, new_tp_price)


async def async_update_trade_entry(trade_id: int, new_entry_price: float) -> bool:
    return await _run_in_executor(update_trade_entry, trade_id, new_entry_price)


async def async_update_trade_margin(trade_id: int, **kwargs) -> bool:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None,
        lambda: update_trade_margin(trade_id, **kwargs),
    )


async def async_get_daily_stats(date_utc: Optional[str] = None) -> dict:
    return await _run_in_executor(get_daily_stats, date_utc)


async def async_get_open_trades_summary() -> dict:
    return await _run_in_executor(get_open_trades_summary)


async def async_cancel_trade(trade_id: int) -> bool:
    return await _run_in_executor(cancel_trade, trade_id)


def get_pending_trades() -> list[dict]:
    """Ambil semua trade dengan status 'pending' (limit order belum fill)."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM trades WHERE status = 'pending' ORDER BY created_at DESC",
        ).fetchall()
    return rows_to_dicts(rows)


async def async_get_pending_trades() -> list[dict]:
    return await _run_in_executor(get_pending_trades)


async def async_get_pending_trade_for_pair(pair: str) -> Optional[dict]:
    return await _run_in_executor(get_trade_by_pair_and_status, pair, "pending")