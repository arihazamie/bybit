"""
bot/executor/order_sync.py
============================
Callback untuk BitgetWsClient (Step 8) — menyinkronkan status LIVE dari
exchange (order & posisi) ke database lokal + notifikasi Telegram.

ROOT CAUSE bug yang difix di sini:
  main.py memanggil `get_ws_client()` TANPA on_order/on_position callback.
  Akibatnya `_dispatch_order` / `_dispatch_position` di ws_client.py selalu
  hit early-return (`if self._on_order is None: return`) — SETIAP event dari
  Bitget (order dibatalkan manual di web, order fill, posisi closed/
  liquidated) dibuang begitu saja. Database lokal & Telegram tidak pernah
  tahu ada perubahan di exchange, walaupun WebSocket-nya sendiri jalan
  normal dan menerima event itu dengan benar.

Catatan penting:
  watch_orders()/watch_positions() (jalur WebSocket) mengirim SEMUA event,
  termasuk order cancelled & posisi contracts=0 — beda dengan reconciliation
  REST (`_reconcile_once`) yang sengaja skip status itu untuk hindari noise.
  Jadi begitu callback ini terpasang, deteksi cancel/close manual di
  web/app Bitget akan tertangkap REAL-TIME lewat WebSocket.

Batasan desain (karena tabel `trades` tidak menyimpan order_id exchange):
  Matching dilakukan berdasarkan PAIR + status lokal (pending/open), bukan
  order_id. Ini valid selama position_checker tetap menjamin maksimal satu
  trade PENDING dan satu trade OPEN per pair pada satu waktu.
"""

from __future__ import annotations

from core.constants import CloseReason, TradeStatus
from core.logging_setup import get_logger
from db.crud.trades import (
    async_cancel_trade,
    async_close_trade,
    async_get_open_trade_for_pair,
    async_get_pending_trade_for_pair,
    async_update_trade_status,
)
from exchange.bitget.ws_client import OrderEvent, PositionEvent
from notifications.notifier import notify

logger = get_logger(__name__)

# Status ccxt unified yang berarti order sudah tidak aktif lagi di exchange
_CANCELLED_STATUSES = {"canceled", "cancelled", "expired", "rejected"}
_FILLED_STATUSES = {"closed"}  # ccxt unified: 'closed' == fully filled untuk order


# ── Order events ──────────────────────────────────────────────────────────

async def on_order_event(event: OrderEvent) -> None:
    """
    Dipanggil BitgetWsClient setiap ada update order — termasuk order yang
    dibatalkan manual di web/app Bitget (bukan cuma order yang dibuat bot ini).
    """
    status = (event.status or "").lower()

    try:
        if status in _CANCELLED_STATUSES:
            await _handle_order_cancelled(event)
        elif status in _FILLED_STATUSES:
            await _handle_order_filled(event)
        # status 'open' — limit order masih nunggu fill, normal, no-op.
    except Exception:
        logger.exception(
            "[order_sync] Gagal proses order event %s %s status=%s",
            event.symbol, event.order_id, status,
        )


async def _handle_order_cancelled(event: OrderEvent) -> None:
    trade = await async_get_pending_trade_for_pair(event.symbol)
    if trade is None:
        return  # tidak ada trade PENDING lokal untuk pair ini — no-op

    ok = await async_cancel_trade(trade["id"])
    if not ok:
        logger.warning(
            "[order_sync] Gagal update trade #%s ke CANCELLED (pair=%s)",
            trade["id"], event.symbol,
        )
        return

    logger.info(
        "[order_sync] Trade #%s (%s) → CANCELLED, terdeteksi dari exchange "
        "(order_id=%s, source=%s)",
        trade["id"], event.symbol, event.order_id, event.source,
    )
    await notify(
        f"🚫 <b>Order dibatalkan</b>\n\n"
        f"Pair    : <code>{event.symbol}</code>\n"
        f"Trade   : #{trade['id']}\n"
        f"Entry   : <code>{trade.get('entry_price', '?')}</code>\n\n"
        f"<i>Terdeteksi dari exchange — kemungkinan dibatalkan manual di "
        f"web/app Bitget. Status database sudah disesuaikan ke CANCELLED.</i>"
    )


async def _handle_order_filled(event: OrderEvent) -> None:
    trade = await async_get_pending_trade_for_pair(event.symbol)
    if trade is None:
        return  # bukan limit order pending kita / sudah diproses sebelumnya

    ok = await async_update_trade_status(trade["id"], TradeStatus.OPEN)
    if not ok:
        logger.warning(
            "[order_sync] Gagal update trade #%s ke OPEN (pair=%s)",
            trade["id"], event.symbol,
        )
        return

    fill_price = event.average or event.price
    logger.info(
        "[order_sync] Trade #%s (%s) → OPEN (fill), order_id=%s source=%s",
        trade["id"], event.symbol, event.order_id, event.source,
    )
    await notify(
        f"✅ <b>Limit order fill</b>\n\n"
        f"Pair    : <code>{event.symbol}</code>\n"
        f"Trade   : #{trade['id']}\n"
        f"Harga   : <code>{fill_price}</code>\n\n"
        f"<i>Posisi sekarang berstatus OPEN.</i>"
    )


# ── Position events ──────────────────────────────────────────────────────

async def on_position_event(event: PositionEvent) -> None:
    """
    Dipanggil BitgetWsClient setiap ada update posisi. Yang paling penting
    di sini adalah kasus contracts == 0 — artinya posisi baru saja closed
    di exchange (SL/TP hit, ditutup manual di web, atau liquidated) TANPA
    lewat perintah bot ini.
    """
    try:
        if event.contracts == 0:
            await _handle_position_closed(event)
        # contracts > 0 — posisi masih berjalan; update live data (liq price,
        # unrealized pnl) opsional, tidak wajib untuk fix bug cancel/close ini.
    except Exception:
        logger.exception(
            "[order_sync] Gagal proses position event %s", event.symbol,
        )


async def _handle_position_closed(event: PositionEvent) -> None:
    trade = await async_get_open_trade_for_pair(event.symbol)
    if trade is None:
        return  # tidak ada trade OPEN lokal untuk pair ini — no-op

    ok = await async_close_trade(
        trade["id"],
        close_reason=CloseReason.MANUAL,
        pnl=None,
        r_multiple=None,
    )
    if not ok:
        return

    logger.warning(
        "[order_sync] Trade #%s (%s) → CLOSED, posisi hilang dari exchange "
        "(source=%s) — pnl TIDAK diketahui pasti, cek manual ke histori Bitget.",
        trade["id"], event.symbol, event.source,
    )
    await notify(
        f"⚠️ <b>Posisi ditutup di exchange</b>\n\n"
        f"Pair    : <code>{event.symbol}</code>\n"
        f"Trade   : #{trade['id']}\n\n"
        f"<i>Posisi ini sudah tidak ada lagi di Bitget (kena SL/TP, ditutup "
        f"manual, atau liquidated). Status database sudah ditandai CLOSED — "
        f"P&amp;L pasti belum diketahui bot, cek langsung riwayat di Bitget.</i>"
    )