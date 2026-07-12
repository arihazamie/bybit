"""
bot/executor/order_sync.py
============================
Callback untuk BitgetWsClient (Step 8) — menyinkronkan status LIVE dari
exchange (order & posisi) ke database lokal + notifikasi Telegram REALTIME
dan profesional, untuk SEMUA cara sebuah trade bisa berakhir:

  - SL_HIT      : stop loss trigger order kena fill
  - TP_HIT      : take profit trigger order kena fill
  - LIQUIDATED  : posisi kena liquidation paksa dari exchange
  - MANUAL      : ditutup manual oleh user (web/app Bitget) — baik posisi
                  open (close position) maupun limit entry yang masih
                  pending (cancel order)
  - SL_AMENDED  : stop loss diubah levelnya (bukan closed) — biasanya user
                  geser harga SL manual di web/app Bitget selagi posisi
                  masih open. TIDAK menutup posisi, hanya update sl_price
                  di database supaya tetap sinkron dengan exchange.
  - SL_REMOVED  : order SL dibatalkan di exchange TANPA order SL pengganti
                  (posisi jadi tanpa proteksi) — dikirim sebagai peringatan
                  darurat, database TIDAK diubah (kolom sl_price NOT NULL,
                  nilai lama disimpan hanya sebagai referensi terakhir).

Cakupan (root cause yang difix di sini + exchange/bitget/ws_client.py):

  1. main.py sekarang MEMANGGIL get_ws_client() DENGAN on_order/on_position
     callback (Step 19) — tanpa ini semua event dari Bitget dibuang begitu
     saja oleh _dispatch_order/_dispatch_position di ws_client.py.

  2. ws_client.py sekarang punya "known-open cache" + diffing di
     _reconcile_once(): kalau sebuah posisi/pending order yang SEBELUMNYA
     diketahui open tiba-tiba HILANG dari snapshot REST tanpa pernah ada
     event WS eksplisit (mis. koneksi WS putus TEPAT saat user close/cancel
     manual di web/app — kasus yang sebelumnya membuat bot "tidak tahu"),
     event closed/cancelled tetap di-dispatch secara sintetis. Jadi deteksi
     TIDAK 100% bergantung pada WebSocket tidak pernah putus.

  3. Modul ini (order_sync.py) yang menerima event tsb TIDAK LAGI cuma
     menandai "manual_close" generik — begitu posisi terdeteksi closed, ia
     query histori order REAL dari exchange (fetch_closed_orders) untuk
     menemukan order penutup yang sesungguhnya, lalu mengklasifikasikan
     close_reason (SL/TP/liquidated/manual) berdasarkan trigger price order
     itu vs sl_price/tp_price/liquidation_price_estimate di database, dan
     menghitung PnL + R-multiple senyata mungkin — bukan cuma "cek manual
     ke histori Bitget".

Batasan desain (karena tabel `trades` tidak menyimpan order_id exchange):
  Matching dilakukan berdasarkan PAIR + status lokal (pending/open), bukan
  order_id. Ini valid selama position_checker tetap menjamin maksimal satu
  trade PENDING dan satu trade OPEN per pair pada satu waktu.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from config.settings import settings
from core.constants import CLOSE_PRICE_MATCH_TOLERANCE_PCT, CloseReason, TradeStatus
from core.logging_setup import get_logger
from db.crud.trades import (
    async_cancel_trade,
    async_close_trade,
    async_get_open_trade_for_pair,
    async_get_open_trades,
    async_get_pending_trade_for_pair,
    async_get_pending_trades,
    async_update_trade_sl,
    async_update_trade_status,
)
from exchange.bitget.retry import CriticalError, TransientError
from exchange.bitget.rest_client import BitgetRestClient, get_rest_client
from exchange.bitget.ws_client import OrderEvent, PositionEvent
from notifications.notifier import notify

from bot.executor.order_manager import set_stop_loss

logger = get_logger(__name__)

# Status ccxt unified yang berarti order sudah tidak aktif lagi di exchange
_CANCELLED_STATUSES = {"canceled", "cancelled", "expired", "rejected"}
_FILLED_STATUSES = {"closed"}  # ccxt unified: 'closed' == fully filled untuk order


def _f(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _pct_diff(a: Optional[float], b: Optional[float]) -> Optional[float]:
    """Selisih % antara dua harga (relatif terhadap b). None kalau salah satu kosong/0."""
    if a is None or b is None or b == 0:
        return None
    return abs(a - b) / abs(b) * 100.0


# ── Order events ──────────────────────────────────────────────────────────

def _is_stop_loss_order(event: OrderEvent) -> bool:
    """
    Heuristik untuk mengenali order SL (stop/trigger order) di antara semua
    order event yang masuk: SL yang dipasang bot ini (lihat
    order_manager.set_stop_loss) SELALU reduce_only=True DENGAN
    trigger_price terisi (stop_market/stop order, params triggerPrice +
    reduceOnly=True, side kebalikan posisi) — order entry biasa (limit/
    market) tidak reduce_only dan tidak punya trigger_price.

    Pola ini juga cocok untuk SL yang dipasang/diubah MANUAL di web/app
    Bitget (trigger order Bitget Futures selalu berbentuk sama di ccxt
    apapun asalnya) — makanya heuristik ini dipakai untuk mendeteksi SL
    yang diubah/dibatalkan manual, bukan hanya SL bikinan bot.
    """
    return bool(event.reduce_only) and event.trigger_price is not None


async def on_order_event(event: OrderEvent) -> None:
    """
    Dipanggil BitgetWsClient setiap ada update order — termasuk order yang
    dibatalkan manual di web/app Bitget (bukan cuma order yang dibuat bot ini),
    dan termasuk event sintetis dari reconciliation kalau event WS asli
    ter-drop (lihat ws_client._dispatch_synthetic_vanished_order).
    """
    status = (event.status or "").lower()

    try:
        if _is_stop_loss_order(event):
            # Order SL (reduce-only trigger order) — dicek terpisah dari
            # order entry biasa, karena 'open' di sini bukan no-op seperti
            # limit entry (SL 'open' = proteksi aktif, levelnya bisa
            # berubah kapan saja lewat amend manual di web/app) dan
            # 'cancelled' di sini bukan berarti sinyal dibatalkan, tapi
            # posisi kehilangan proteksi.
            await _handle_sl_order_event(event, status)
            return

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
        trade["id"], event.symbol, event.order_id or "-", event.source,
    )
    await notify(
        f"🚫 <b>LIMIT ENTRY DIBATALKAN</b>\n\n"
        f"Pair    : <code>{event.symbol}</code>\n"
        f"Trade   : #{trade['id']}\n"
        f"Entry   : <code>{trade.get('entry_price', '?')}</code>\n\n"
        f"<i>Terdeteksi realtime dari exchange — kemungkinan dibatalkan manual "
        f"di web/app Bitget. Status database sudah disesuaikan ke CANCELLED.</i>"
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
    fill_price_display = f"{fill_price:g}" if fill_price is not None else "tidak diketahui (terverifikasi via reconciliation)"
    logger.info(
        "[order_sync] Trade #%s (%s) → OPEN (fill), order_id=%s source=%s",
        trade["id"], event.symbol, event.order_id or "-", event.source,
    )
    # NB: SL TIDAK diset di sini lagi — sekarang dipasang LANGSUNG saat entry
    # order dikirim (lihat signal_pipeline.py, dieksekusi bersamaan dengan
    # entry, tidak menunggu fill event ini). Kalau di-set lagi di sini juga,
    # hasilnya DOBEL SL order untuk trade yang sama. Handler ini sekarang
    # murni update status + notifikasi informasi fill.
    await notify(
        f"✅ <b>LIMIT ORDER FILLED</b>\n\n"
        f"Pair    : <code>{event.symbol}</code>\n"
        f"Trade   : #{trade['id']}\n"
        f"Harga   : <code>{fill_price_display}</code>\n\n"
        f"<i>Posisi sekarang berstatus OPEN. SL sudah terpasang sejak entry "
        f"dikirim — cek notifikasi \"SL TERPASANG\" sebelumnya.</i>"
    )


# ── SL order events (amend/cancel manual di web/app) ─────────────────────

async def _handle_sl_order_event(event: OrderEvent, status: str) -> None:
    """
    Router untuk event order SL (reduce-only trigger order) — dipanggil dari
    on_order_event ketika _is_stop_loss_order(event) True.

    - status == 'open'        → SL masih aktif; levelnya dibandingkan dengan
                                 sl_price di database — kalau beda berarti
                                 diamend (mis. digeser manual di web/app),
                                 sinkronkan database ke level terbaru.
    - status in cancelled set → SL order hilang dari exchange TANPA order
                                 pengganti yang terdeteksi bareng event ini —
                                 posisi kehilangan proteksi, kirim peringatan.
    - status == 'closed' (filled) → SL KENA (trigger tereksekusi). Ini sudah
                                 tercover oleh on_position_event (contracts
                                 jadi 0 → _handle_position_closed mengklasi-
                                 fikasikannya sebagai SL_HIT lewat histori
                                 order), jadi sengaja no-op di sini supaya
                                 tidak dobel notifikasi.
    """
    if status == "open":
        await _handle_sl_order_open(event)
    elif status in _CANCELLED_STATUSES:
        await _handle_sl_order_cancelled(event)
    # status 'closed' (filled/triggered) — no-op, lihat docstring di atas.


async def _handle_sl_order_open(event: OrderEvent) -> None:
    """
    SL order live dengan trigger_price tertentu — cross-check dengan
    sl_price yang tersimpan di trade OPEN untuk pair ini. Kalau beda
    (di luar toleransi CLOSE_PRICE_MATCH_TOLERANCE_PCT), berarti SL baru
    saja diamend (paling sering: digeser manual di web/app Bitget) —
    update database supaya tetap sinkron dengan exchange (source of truth).
    """
    if event.trigger_price is None:
        return

    trade = await async_get_open_trade_for_pair(event.symbol)
    if trade is None:
        return  # tidak ada trade OPEN lokal untuk pair ini — bukan SL yang kita lacak

    current_sl = _f(trade.get("sl_price"))
    diff_pct = _pct_diff(event.trigger_price, current_sl)
    # NB: sama seperti _classify_close_reason — jangan pakai `diff or 999`,
    # karena _pct_diff bisa return 0.0 (exact match) yang falsy di Python.
    if diff_pct is not None and diff_pct <= CLOSE_PRICE_MATCH_TOLERANCE_PCT:
        return  # sama dalam toleransi — tidak ada perubahan riil, no-op

    ok = await async_update_trade_sl(trade["id"], event.trigger_price)
    if not ok:
        logger.warning(
            "[order_sync] Gagal update SL trade #%s ke %s (pair=%s)",
            trade["id"], event.trigger_price, event.symbol,
        )
        return

    old_sl_display = f"{current_sl:g}" if current_sl is not None else "belum diset"
    logger.info(
        "[order_sync] Trade #%s (%s) → SL diupdate %s → %s, terdeteksi realtime "
        "dari exchange (order_id=%s, source=%s)",
        trade["id"], event.symbol, old_sl_display, event.trigger_price,
        event.order_id or "-", event.source,
    )
    await notify(
        f"🛡️ <b>STOP LOSS DIUBAH</b>\n\n"
        f"Pair    : <code>{event.symbol}</code>\n"
        f"Trade   : #{trade['id']}\n"
        f"SL Lama : <code>{old_sl_display}</code>\n"
        f"SL Baru : <code>{event.trigger_price:g}</code>\n\n"
        f"<i>Terdeteksi realtime dari exchange — kemungkinan diubah manual di "
        f"web/app Bitget. Database sudah disesuaikan ke level terbaru.</i>"
    )


async def _handle_sl_order_cancelled(event: OrderEvent) -> None:
    """
    SL order hilang (cancelled/expired) dari exchange untuk pair yang masih
    punya trade OPEN di database. TIDAK menyentuh sl_price di database
    (kolom NOT NULL, dan reconciliation berikutnya tidak boleh salah kira
    "belum pernah ada SL" jadi 0/kosong) — nilai lama tetap tersimpan hanya
    sebagai referensi historis. Ini murni peringatan darurat supaya user
    sadar posisinya sekarang TANPA proteksi stop loss aktif di exchange.
    """
    trade = await async_get_open_trade_for_pair(event.symbol)
    if trade is None:
        return  # tidak ada trade OPEN lokal — no-op

    last_sl = trade.get("sl_price")
    logger.warning(
        "[order_sync] Trade #%s (%s) → SL order DIBATALKAN di exchange "
        "(order_id=%s, source=%s) — posisi SEKARANG TANPA proteksi stop "
        "loss aktif! sl_price di database (%s) TIDAK diubah, hanya referensi terakhir.",
        trade["id"], event.symbol, event.order_id or "-", event.source, last_sl,
    )
    await notify(
        f"🚨 <b>PERINGATAN: STOP LOSS DIBATALKAN</b>\n\n"
        f"Pair          : <code>{event.symbol}</code>\n"
        f"Trade         : #{trade['id']}\n"
        f"SL terakhir   : <code>{last_sl if last_sl is not None else '?'}</code>\n\n"
        f"<i>Order SL terdeteksi dibatalkan di exchange — kemungkinan dihapus manual "
        f"di web/app Bitget. Posisi ini SEKARANG TIDAK punya stop loss aktif. "
        f"Segera pasang ulang SL kalau ini tidak disengaja.</i>"
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


@dataclass
class CloseDetail:
    """Hasil klasifikasi close_reason + PnL untuk satu posisi yang baru closed."""
    reason: str = CloseReason.MANUAL
    exit_price: Optional[float] = None
    pnl: Optional[float] = None
    r_multiple: Optional[float] = None
    verified: bool = False   # True kalau berhasil dikonfirmasi via histori order exchange


def _pick_closing_order(orders: List[Dict[str, Any]], direction: str) -> Optional[Dict[str, Any]]:
    """
    Dari daftar closed order (REST), cari order yang paling mungkin menjadi
    penutup posisi: side berlawanan dengan arah trade, status filled, dan
    yang paling BARU (timestamp terbesar).
    """
    closing_side = "sell" if direction == "long" else "buy"
    candidates = [
        o for o in orders
        if (o.get("status") == "closed")
        and (o.get("side") == closing_side)
        and (_f(o.get("filled")) or 0.0) > 0
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda o: _f(o.get("timestamp")) or 0.0, reverse=True)
    return candidates[0]


def _extract_reported_pnl(order: Dict[str, Any]) -> Optional[float]:
    """Coba ambil realized PnL langsung dari raw info Bitget kalau exchange menyediakannya."""
    info = order.get("info") or {}
    for key in ("pnl", "profit", "realizedPnl", "totalProfits", "achievedProfits"):
        value = _f(info.get(key))
        if value is not None:
            return value
    return None


def _classify_close_reason(
    exit_price: Optional[float],
    trade: dict,
    trigger_price: Optional[float],
) -> str:
    """
    Klasifikasikan close_reason berdasarkan harga exit real vs sl_price/
    tp_price/liquidation_price_estimate tersimpan di database (toleransi
    CLOSE_PRICE_MATCH_TOLERANCE_PCT %), murni — tidak ada I/O.
    """
    reference_price = trigger_price if trigger_price is not None else exit_price
    if reference_price is None:
        return CloseReason.MANUAL

    tp = trade.get("tp_price")
    sl = trade.get("sl_price")
    liq = trade.get("liquidation_price_estimate")

    def _within_tolerance(target: Optional[float]) -> bool:
        # NB: jangan pakai `x or 999` di sini — _pct_diff bisa return 0.0
        # (exact match), dan 0.0 itu falsy di Python sehingga akan salah
        # ke-treat sebagai "999" (di luar toleransi).
        diff = _pct_diff(reference_price, target)
        return diff is not None and diff <= CLOSE_PRICE_MATCH_TOLERANCE_PCT

    if tp is not None and _within_tolerance(tp):
        return CloseReason.TP_HIT
    if liq is not None and _within_tolerance(liq):
        return CloseReason.LIQUIDATED
    if sl is not None and _within_tolerance(sl):
        return CloseReason.SL_HIT
    return CloseReason.MANUAL


async def _resolve_close_detail(symbol: str, trade: dict, rest_client: BitgetRestClient) -> CloseDetail:
    """
    Cari tahu SEBENARNYA apa yang terjadi ke posisi yang baru closed —
    query histori order (fetch_closed_orders) untuk menemukan order penutup
    real, lalu klasifikasikan close_reason + hitung PnL/R-multiple.

    Tidak pernah raise — kalau REST gagal/order tidak ketemu, fallback ke
    CloseDetail(reason=MANUAL, verified=False) supaya database tetap
    ke-update (lebih baik "manual" yang tidak pasti daripada trade nyangkut
    status OPEN selamanya).
    """
    direction = trade.get("direction", "long")
    entry_price = _f(trade.get("entry_price"))
    position_size = _f(trade.get("position_size"))
    risk_amount_usd = _f(trade.get("risk_amount_usd"))

    try:
        closed_orders = await rest_client.fetch_closed_orders(symbol, limit=10)
    except (CriticalError, TransientError) as exc:
        logger.warning(
            "[order_sync] Gagal fetch_closed_orders untuk %s (%s) — fallback close_reason=manual, "
            "PnL tidak diketahui pasti.", symbol, exc,
        )
        return CloseDetail(reason=CloseReason.MANUAL, verified=False)

    closing_order = _pick_closing_order(closed_orders, direction)
    if closing_order is None:
        return CloseDetail(reason=CloseReason.MANUAL, verified=False)

    exit_price = _f(closing_order.get("average")) or _f(closing_order.get("price"))
    trigger_price = _f(closing_order.get("triggerPrice")) or _f(closing_order.get("stopPrice"))
    reason = _classify_close_reason(exit_price, trade, trigger_price)

    pnl = _extract_reported_pnl(closing_order)
    if pnl is None and entry_price is not None and position_size is not None and exit_price is not None:
        sign = 1.0 if direction == "long" else -1.0
        pnl = (exit_price - entry_price) * position_size * sign

    r_multiple = None
    if pnl is not None and risk_amount_usd:
        r_multiple = round(pnl / risk_amount_usd, 2)

    return CloseDetail(
        reason=reason,
        exit_price=exit_price,
        pnl=round(pnl, 4) if pnl is not None else None,
        r_multiple=r_multiple,
        verified=True,
    )


_REASON_LABEL = {
    CloseReason.SL_HIT: ("🔴", "STOP LOSS TERKENA"),
    CloseReason.TP_HIT: ("🟢", "TAKE PROFIT TERCAPAI"),
    CloseReason.LIQUIDATED: ("💀", "POSISI TERLIKUIDASI"),
    CloseReason.MANUAL: ("✋", "POSISI DITUTUP MANUAL"),
}


def _format_close_notification(trade: dict, detail: CloseDetail, symbol: str) -> str:
    emoji, label = _REASON_LABEL.get(detail.reason, ("⚠️", "POSISI DITUTUP"))

    lines = [f"{emoji} <b>{label}</b>", ""]
    lines.append(f"Pair    : <code>{symbol}</code>")
    lines.append(f"Trade   : #{trade['id']} ({trade.get('direction', '?').upper()})")
    lines.append(f"Entry   : <code>{trade.get('entry_price', '?')}</code>")
    if detail.exit_price is not None:
        lines.append(f"Exit    : <code>{detail.exit_price:g}</code>")

    if detail.pnl is not None:
        sign = "+" if detail.pnl >= 0 else ""
        lines.append(f"PnL     : <b>{sign}{detail.pnl:.4f} USDT</b>")
    if detail.r_multiple is not None:
        sign = "+" if detail.r_multiple >= 0 else ""
        lines.append(f"R-Mult  : <b>{sign}{detail.r_multiple:.2f}R</b>")

    analyst = trade.get("source_analyst")
    if analyst:
        lines.append(f"Analyst : {analyst}")

    lines.append("")
    if not detail.verified:
        lines.append(
            "<i>Terdeteksi realtime dari exchange, tapi order penutup tidak berhasil "
            "dikonfirmasi via histori — close_reason & PnL di atas adalah estimasi "
            "terbaik. Cek histori Bitget untuk angka pasti.</i>"
        )
    else:
        lines.append("<i>Terdeteksi & terverifikasi realtime dari histori order exchange.</i>")

    return "\n".join(lines)


async def _handle_position_closed(event: PositionEvent) -> None:
    trade = await async_get_open_trade_for_pair(event.symbol)
    if trade is None:
        return  # tidak ada trade OPEN lokal untuk pair ini — no-op

    detail = await _resolve_close_detail(event.symbol, trade, get_rest_client())

    ok = await async_close_trade(
        trade["id"],
        close_reason=detail.reason,
        pnl=detail.pnl,
        r_multiple=detail.r_multiple,
    )
    if not ok:
        logger.warning(
            "[order_sync] Gagal update trade #%s ke CLOSED (pair=%s)",
            trade["id"], event.symbol,
        )
        return

    logger.warning(
        "[order_sync] Trade #%s (%s) → CLOSED reason=%s exit=%s pnl=%s R=%s "
        "(source=%s, verified=%s)",
        trade["id"], event.symbol, detail.reason, detail.exit_price,
        detail.pnl, detail.r_multiple, event.source, detail.verified,
    )
    await notify(_format_close_notification(trade, detail, event.symbol))


# ── Startup reconciliation ─────────────────────────────────────────────────
# Menutup gap terakhir: kalau posisi/order sudah closed/cancelled SAAT bot
# mati (downtime), reconciliation ws_client tidak bisa mendeteksinya sendiri
# karena cache "known open"-nya kosong di awal. Dua fungsi ini dipanggil dari
# main.py SEBELUM ws.start() untuk (a) seed cache itu dari database, dan (b)
# langsung proses trade yang ternyata sudah tidak ada lagi live di exchange.

async def get_known_open_symbols() -> tuple[set[str], set[str]]:
    """Return (open_position_symbols, pending_order_symbols) dari database — dipakai untuk seed ws_client."""
    open_trades = await async_get_open_trades()
    pending_trades = await async_get_pending_trades()
    return (
        {t["pair"] for t in open_trades},
        {t["pair"] for t in pending_trades},
    )


async def reconcile_on_startup(rest_client: Optional[BitgetRestClient] = None) -> None:
    """
    Sinkronisasi satu kali saat bot startup: untuk tiap trade OPEN/PENDING di
    database, cek apakah posisi/order itu MASIH benar-benar ada di exchange.
    Kalau tidak — berarti closed/cancelled SAAT bot offline — proses lewat
    jalur yang sama seperti event realtime (on_position_event/on_order_event),
    supaya history/PnL/notifikasi tetap konsisten.
    """
    client = rest_client or get_rest_client()

    try:
        live_positions = await client.fetch_positions()
        live_orders = await client.fetch_open_orders()
    except (CriticalError, TransientError) as exc:
        logger.error("[order_sync] Startup reconciliation gagal fetch live state: %s", exc)
        return

    live_position_symbols = {
        p.get("symbol") for p in live_positions if (_f(p.get("contracts")) or 0.0) > 0
    }
    live_order_symbols = {o.get("symbol") for o in live_orders if o.get("symbol")}

    open_trades = await async_get_open_trades()
    pending_trades = await async_get_pending_trades()

    for trade in open_trades:
        if trade.get("status") != TradeStatus.OPEN:
            continue
        pair = trade["pair"]
        if pair not in live_position_symbols:
            logger.warning(
                "[order_sync] Startup reconciliation: trade OPEN #%s (%s) sudah tidak "
                "ada lagi di exchange (closed saat bot offline) — memproses close.",
                trade["id"], pair,
            )
            await on_position_event(PositionEvent(
                symbol=pair, side=None, contracts=0.0, entry_price=None,
                mark_price=None, liquidation_price=None, unrealized_pnl=None,
                leverage=None, margin_mode=None, timestamp_ms=None,
                source="startup_reconciliation", raw={},
            ))

    for trade in pending_trades:
        if trade.get("status") != TradeStatus.PENDING:
            continue
        pair = trade["pair"]
        if pair not in live_order_symbols:
            filled = pair in live_position_symbols
            logger.warning(
                "[order_sync] Startup reconciliation: trade PENDING #%s (%s) sudah "
                "tidak ada lagi di open orders exchange saat bot offline — "
                "diklasifikasikan sebagai %s.",
                trade["id"], pair, "filled" if filled else "cancelled",
            )
            await on_order_event(OrderEvent(
                symbol=pair, order_id="", status="closed" if filled else "canceled",
                side=None, order_type=None, price=None, average=None,
                filled=0.0, remaining=0.0, trigger_price=None, reduce_only=False,
                timestamp_ms=None, source="startup_reconciliation", raw={},
            ))