"""
bot/executor/order_manager.py
==============================
Step 13 — Bitget executor: SL, close & manajemen order.

Tanggung jawab:
  1. set_stop_loss()      — Set SL otomatis setelah entry fill via
                            trigger/stop order Bitget Futures (wajib dipanggil
                            segera setelah open_position berhasil)
  2. cancel_pending_order() — Cancel limit order yang belum fill
  3. close_position()     — Close posisi by market (manual atau dari command /close)
  4. close_all_positions() — Emergency close semua posisi open

Semua fungsi:
  - Update status di database (tabel trades, event_log)
  - Kirim teks notifikasi yang bisa langsung diteruskan ke Telegram (Step 15+)
  - Dry-run mode: log aksi tanpa menyentuh exchange
  - Klasifikasi error konsisten dengan Step 12:
      CriticalError  → caller trip circuit breaker ORDER_EXECUTION
      TransientError → caller bisa retry

Scope yang TIDAK dikerjakan di sini:
  - Circuit breaker state machine  → Step 14
  - Telegram inline button          → Step 18
  - Watch order fill event (WS)     → Step 8 (ws_client) + Step 19 (pipeline)

Catatan Bitget Futures — Stop Loss order:
  Bitget Futures punya DUA jenis order "trigger" yang gampang tertukar:

  1. "Plan order" biasa (endpoint place-plan-order, ccxt: params.triggerPrice)
     — order kondisional BERDIRI SENDIRI, tidak terikat ke posisi. Skema-nya
     butuh 'side'+'tradeSide' (open/close) yang jelas, dan setiap panggilan
     create_order() dengan triggerPrice akan membuat order BARU — tidak
     pernah menggantikan order lama. Kalau dipakai berulang untuk SL (mis.
     tiap kali /setsl dipanggil), order reduce-only akan MENUMPUK di
     exchange untuk symbol yang sama, dan cepat atau lambat exchange
     menolak order berikutnya (parameter/posisi tidak konsisten) — muncul
     sebagai error generik {"code":"400172","msg":"The order type is
     illegal"} yang membingungkan karena sebenarnya bukan soal `orderType`.
  2. "TPSL order" (endpoint place-tpsl-order, ccxt: params.stopLossPrice)
     — order yang TERIKAT ke posisi (holdSide 'long'/'short'), inilah
     mekanisme resmi Bitget untuk stop loss sebuah posisi. Skemanya HANYA
     butuh triggerPrice + holdSide + size, tidak butuh side/tradeSide/
     reduceOnly/marginMode sama sekali.

  Implementasi di bawah ini SENGAJA pakai jalur (2) — params.stopLossPrice,
  BUKAN params.triggerPrice — supaya SL benar-benar terikat ke posisi dan
  konsisten dengan cara Bitget sendiri menampilkan/menghitungnya. Order
  type (field `orderType` Bitget) tetap HARUS 'market' atau 'limit' — TIDAK
  PERNAH 'stop_market'/'stop' (itu bug lama yang sudah dibuang duluan).

  ccxt bitget createOrder() menolak params yang berisi lebih dari satu dari
  triggerPrice/stopLossPrice/takeProfitPrice/trailingPercent sekaligus —
  jadi params di sini HANYA berisi 'stopLossPrice'.

  Params yang dikirim ke create_order(type='market', params=...):
    - 'stopLossPrice' : harga trigger SL (SATU-SATUNYA dari keempat opsi
                         exclusive yang dipakai di sini)
    - 'holdSide'      : 'long' | 'short' — arah posisi yang di-protect
    - 'triggerType'   : 'mark_price' (default Bitget, eksplisit di sini)
    - 'productType'   : 'USDT-FUTURES'

  Karena TPSL order TIDAK auto-replace saat dipanggil ulang (order lama
  tetap ada di exchange kalau tidak di-cancel), set_stop_loss() di bawah
  selalu MENCOBA cancel sl_order_id lama (tersimpan di DB, kolom
  trades.sl_order_id) SEBELUM memasang SL baru — supaya /setsl benar-benar
  "update", bukan menambah order baru yang menumpuk.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import ccxt

from config.settings import settings
from core.constants import (
    CloseReason,
    Component,
    Direction,
    EventType,
    Severity,
    TradeStatus,
)
from core.logging_setup import get_logger
from db.crud.event_log import async_log_event
from db.crud.trades import (
    async_cancel_trade,
    async_close_trade,
    async_get_open_trades,
    async_get_trade_by_id,
    async_update_trade_sl,
    async_update_trade_status,
)
from exchange.bitget.rest_client import BitgetRestClient, get_rest_client
from exchange.bitget.retry import CriticalError, TransientError

logger = get_logger(__name__)


# ── Result dataclass ─────────────────────────────────────────────────────────

@dataclass
class OrderManagementResult:
    """
    Hasil operasi manajemen order/posisi — dikembalikan ke pipeline / command handler.

    success=True  → operasi berhasil (atau dry_run)
    success=False → gagal; is_critical=True → trip circuit breaker
    """
    success: bool
    operation: str        # 'set_sl' | 'cancel_order' | 'close_position' | 'close_all'
    pair: Optional[str] = None
    trade_id: Optional[int] = None

    # Detail operasi
    sl_price: Optional[float] = None          # untuk set_sl
    sl_order_id: Optional[str] = None         # id stop order di exchange
    closed_pnl: Optional[float] = None        # untuk close (estimasi, bukan final)
    cancelled_order_id: Optional[str] = None  # untuk cancel

    closed_pairs: List[str] = field(default_factory=list)   # untuk close_all
    failed_pairs: List[str] = field(default_factory=list)   # untuk close_all

    is_dry_run: bool = False
    failure_reason: Optional[str] = None
    is_critical: bool = False
    notes: list = field(default_factory=list)

    def notification_text(self) -> str:
        return format_order_management_notification(self)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _opposite_side(direction: str) -> str:
    """Close order harus di sisi berlawanan dari posisi."""
    return "sell" if direction == Direction.LONG else "buy"


def _hold_side(direction: str) -> str:
    return "long" if direction == Direction.LONG else "short"


def _parse_order_id(raw: Dict[str, Any]) -> str:
    return str(
        raw.get("id")
        or raw.get("orderId")
        or raw.get("info", {}).get("orderId")
        or ""
    )


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


# ── 1. Set Stop Loss ─────────────────────────────────────────────────────────

async def _place_sl_order(
    client: BitgetRestClient,
    symbol: str,
    direction: str,
    position_size: float,
    sl_price: float,
    dry_run: bool,
) -> Dict[str, Any]:
    """
    Kirim SL order (TPSL, terikat ke posisi) ke Bitget Futures.

    Dry-run: return stub tanpa menyentuh exchange.
    Raise CriticalError / TransientError ke caller.
    """
    if dry_run:
        logger.info(
            "[order_manager][DRY-RUN] Would place SL TPSL order: "
            "%s %s stopLossPrice=%g size=%g",
            symbol, _hold_side(direction), sl_price, position_size,
        )
        return {"id": "DRY_RUN_SL", "symbol": symbol}

    side = _opposite_side(direction)
    hold_side = _hold_side(direction)

    try:
        exchange = await client._get_exchange()

        # params.stopLossPrice → ccxt bitget route ke endpoint place-tpsl-order
        # (SL yang TERIKAT ke posisi via holdSide) — BUKAN params.triggerPrice
        # yang route ke place-plan-order (order kondisional berdiri sendiri).
        # Lihat penjelasan lengkap di docstring modul ini.
        raw = await exchange.create_order(
            symbol=symbol,
            type="market",
            side=side,
            amount=position_size,
            price=None,
            params={
                "stopLossPrice": sl_price,
                "holdSide": hold_side,
                "triggerType": "mark_price",
                "productType": "USDT-FUTURES",
            },
        )
        logger.info(
            "[order_manager] SL TPSL order placed: %s trigger=%g → id=%s",
            symbol, sl_price, _parse_order_id(raw),
        )
        return raw

    except (CriticalError, TransientError):
        raise
    except ccxt.InvalidOrder as exc:
        raise CriticalError(
            f"[order_manager] SL order ditolak exchange untuk {symbol}: {exc}",
            original=exc,
        ) from exc
    except ccxt.InsufficientFunds as exc:
        raise CriticalError(
            f"[order_manager] Insufficient funds saat set SL {symbol}: {exc}",
            original=exc,
        ) from exc
    except ccxt.AuthenticationError as exc:
        raise CriticalError(
            f"[order_manager] Auth error saat set SL {symbol}: {exc}", original=exc
        ) from exc
    except ccxt.NetworkError as exc:
        raise TransientError(
            f"[order_manager] Network error saat set SL {symbol}: {exc}", original=exc
        ) from exc
    except ccxt.RequestTimeout as exc:
        raise TransientError(
            f"[order_manager] Timeout saat set SL {symbol}: {exc}", original=exc
        ) from exc
    except ccxt.RateLimitExceeded as exc:
        raise TransientError(
            f"[order_manager] Rate limit saat set SL {symbol}: {exc}", original=exc
        ) from exc
    except Exception as exc:
        raise CriticalError(
            f"[order_manager] Unexpected error saat set SL {symbol}: {exc}",
            original=exc,
        ) from exc


async def _cancel_sl_order(
    client: BitgetRestClient,
    symbol: str,
    sl_order_id: str,
    dry_run: bool,
) -> None:
    """
    Cancel SL TPSL order lama di exchange (best-effort, dipanggil sebelum
    memasang SL baru supaya /setsl benar-benar "update" bukan menambah
    order baru yang menumpuk).

    OrderNotFound (SL lama sudah fill/ke-cancel/expired) dianggap sukses.
    Error lain di-log sebagai warning dan TIDAK menghentikan proses —
    kegagalan cancel order lama tidak boleh memblokir pemasangan SL baru.
    """
    if dry_run:
        logger.info(
            "[order_manager][DRY-RUN] Would cancel old SL order %s for %s",
            sl_order_id, symbol,
        )
        return
    try:
        exchange = await client._get_exchange()
        await exchange.cancel_order(
            sl_order_id, symbol,
            params={
                "stop": True,
                "planType": "pos_loss",
                "productType": "USDT-FUTURES",
            },
        )
        logger.info(
            "[order_manager] Old SL order cancelled: %s id=%s", symbol, sl_order_id
        )
    except ccxt.OrderNotFound:
        logger.info(
            "[order_manager] Old SL order %s untuk %s tidak ditemukan "
            "(sudah fill/cancel/expired) — dianggap beres.",
            sl_order_id, symbol,
        )
    except Exception as exc:
        logger.warning(
            "[order_manager] Gagal cancel SL order lama %s untuk %s (non-fatal, "
            "lanjut pasang SL baru): %s",
            sl_order_id, symbol, exc,
        )


def _humanize_exchange_error(exc: Optional[BaseException]) -> str:
    """
    Terjemahkan exception teknis dari ccxt/exchange jadi pesan singkat yang
    enak dibaca di Telegram. Detail teknis lengkap tetap masuk ke log &
    event_log lewat CriticalError/TransientError.__str__ — fungsi ini HANYA
    untuk teks yang ditampilkan ke user.
    """
    if exc is None:
        return "Terjadi kesalahan tidak diketahui saat menghubungi exchange."

    text = str(exc)

    if isinstance(exc, ccxt.AuthenticationError):
        return "Gagal autentikasi ke exchange — cek API key/secret/passphrase bot."
    if isinstance(exc, ccxt.InsufficientFunds):
        return "Saldo margin di exchange tidak cukup untuk operasi ini."
    if isinstance(exc, ccxt.InvalidOrder):
        if "400172" in text:
            return (
                "Exchange menolak order ini (parameter/order tidak valid). "
                "Biasanya karena masih ada order SL lama yang belum bersih di "
                "exchange — tunggu sebentar lalu coba /setsl lagi. Jika terus "
                "terjadi, cek order pending di exchange secara manual."
            )
        return "Order ditolak exchange — parameter order tidak valid untuk posisi/pair ini."
    if isinstance(exc, ccxt.RateLimitExceeded):
        return "Exchange sedang membatasi request (rate limit) — tunggu sebentar lalu coba lagi."
    if isinstance(exc, ccxt.RequestTimeout):
        return "Exchange tidak merespons tepat waktu (timeout) — coba lagi."
    if isinstance(exc, ccxt.NetworkError):
        return "Gangguan koneksi ke exchange — coba lagi dalam beberapa saat."
    if isinstance(exc, ccxt.ExchangeError):
        return "Exchange menolak permintaan ini. Cek log bot untuk detail teknis."
    return "Terjadi kesalahan tak terduga saat menghubungi exchange. Cek log bot untuk detail teknis."


async def set_stop_loss(
    trade_id: int,
    sl_price: float,
    *,
    rest_client: Optional[BitgetRestClient] = None,
    dry_run: Optional[bool] = None,
) -> OrderManagementResult:
    """
    Set Stop Loss otomatis setelah entry fill.

    Dipanggil oleh:
      - Pipeline (Step 19) segera setelah open_position() sukses
      - WebSocket handler (Step 8) saat order fill event terdeteksi
      - Command /setsl dari Telegram (Step 17) untuk update SL posisi open

    Alur:
      1. Fetch trade dari DB untuk dapatkan pair, direction, position_size
      2. Place stop order di exchange
      3. Update sl_price di database
      4. Log event
    """
    is_dry = dry_run if dry_run is not None else settings.DRY_RUN
    client = rest_client or get_rest_client()

    # Fetch trade dari DB
    try:
        trade = await async_get_trade_by_id(trade_id)
    except Exception as exc:
        return OrderManagementResult(
            success=False, operation="set_sl",
            failure_reason=f"db_error: {exc}", is_critical=False,
        )

    if trade is None:
        return OrderManagementResult(
            success=False, operation="set_sl",
            failure_reason=f"trade_not_found: id={trade_id}", is_critical=False,
        )

    pair = trade["pair"]
    direction = trade.get("direction", Direction.LONG)
    position_size = _safe_float(trade.get("position_size"))
    old_sl_order_id = trade.get("sl_order_id")

    if position_size <= 0:
        return OrderManagementResult(
            success=False, operation="set_sl", pair=pair, trade_id=trade_id,
            failure_reason="invalid_position_size: position_size=0", is_critical=False,
        )

    # Cancel SL order lama (kalau ada) SEBELUM pasang yang baru, supaya
    # /setsl benar-benar "update" — bukan menambah order baru yang
    # menumpuk di exchange. Best-effort: kegagalan cancel tidak
    # menghentikan pemasangan SL baru (lihat _cancel_sl_order).
    if old_sl_order_id:
        await _cancel_sl_order(client, pair, old_sl_order_id, is_dry)

    # Place stop order
    try:
        raw = await _place_sl_order(client, pair, direction, position_size, sl_price, is_dry)
    except CriticalError as exc:
        msg = f"Critical error saat set SL {pair} @ {sl_price}: {exc}"
        logger.error("[order_manager] %s", msg)
        await async_log_event(
            EventType.OTHER, msg, component=Component.ORDER_EXECUTION,
            severity=Severity.CRITICAL, trade_id=trade_id,
        )
        return OrderManagementResult(
            success=False, operation="set_sl", pair=pair, trade_id=trade_id,
            failure_reason=_humanize_exchange_error(exc.original or exc), is_critical=True,
        )
    except TransientError as exc:
        msg = f"Transient error saat set SL {pair} @ {sl_price}: {exc}"
        logger.warning("[order_manager] %s", msg)
        await async_log_event(
            EventType.OTHER, msg, component=Component.ORDER_EXECUTION,
            severity=Severity.WARNING, trade_id=trade_id,
        )
        return OrderManagementResult(
            success=False, operation="set_sl", pair=pair, trade_id=trade_id,
            failure_reason=_humanize_exchange_error(exc.original or exc), is_critical=False,
        )

    sl_order_id = _parse_order_id(raw)

    # Update DB (harga + order id baru, supaya /setsl berikutnya bisa cancel ini)
    try:
        await async_update_trade_sl(trade_id, sl_price, sl_order_id or None)
    except Exception as exc:
        logger.warning("[order_manager] DB update SL gagal (non-fatal): %s", exc)

    dry_tag = "[DRY-RUN] " if is_dry else ""
    msg = (
        f"{dry_tag}SL set: {pair} {direction.upper()} @ {sl_price:g} | "
        f"trade_id={trade_id} sl_order_id={sl_order_id or 'N/A'}"
    )
    await async_log_event(
        EventType.OTHER, msg, component=Component.ORDER_EXECUTION,
        severity=Severity.INFO, trade_id=trade_id,
    )

    return OrderManagementResult(
        success=True, operation="set_sl", pair=pair, trade_id=trade_id,
        sl_price=sl_price, sl_order_id=sl_order_id or None,
        is_dry_run=is_dry,
    )


# ── 2. Cancel Pending Order ──────────────────────────────────────────────────

async def _cancel_exchange_order(
    client: BitgetRestClient,
    symbol: str,
    order_id: str,
    dry_run: bool,
) -> None:
    """
    Cancel satu order di exchange.
    Raise CriticalError / TransientError ke caller.
    """
    if dry_run:
        logger.info(
            "[order_manager][DRY-RUN] Would cancel order %s for %s", order_id, symbol
        )
        return

    try:
        exchange = await client._get_exchange()
        await exchange.cancel_order(
            order_id, symbol,
            params={"productType": "USDT-FUTURES"},
        )
        logger.info("[order_manager] Order cancelled: %s id=%s", symbol, order_id)

    except ccxt.OrderNotFound:
        # Order sudah fill atau sudah di-cancel sebelumnya — bukan error fatal
        logger.warning(
            "[order_manager] Order %s untuk %s tidak ditemukan (sudah fill/cancel) "
            "— dianggap berhasil cancel.",
            order_id, symbol,
        )
    except (CriticalError, TransientError):
        raise
    except ccxt.AuthenticationError as exc:
        raise CriticalError(
            f"[order_manager] Auth error saat cancel {symbol}: {exc}", original=exc
        ) from exc
    except ccxt.NetworkError as exc:
        raise TransientError(
            f"[order_manager] Network error saat cancel {symbol}: {exc}", original=exc
        ) from exc
    except ccxt.RequestTimeout as exc:
        raise TransientError(
            f"[order_manager] Timeout saat cancel {symbol}: {exc}", original=exc
        ) from exc
    except Exception as exc:
        raise CriticalError(
            f"[order_manager] Unexpected error saat cancel {symbol}: {exc}",
            original=exc,
        ) from exc


async def cancel_pending_order(
    trade_id: int,
    *,
    exchange_order_id: Optional[str] = None,
    rest_client: Optional[BitgetRestClient] = None,
    dry_run: Optional[bool] = None,
) -> OrderManagementResult:
    """
    Cancel limit order yang belum fill.

    Dipanggil oleh:
      - Command /cancel {pair} dari Telegram (Step 17)
      - Pipeline executor saat conflict_mode=replace dan ada pending order lama

    exchange_order_id: opsional — jika None, fungsi mencoba ambil dari DB.
    Untuk saat ini (Step 13) kalau order_id tidak ada di DB, fallback ke
    fetch_open_orders via rest_client.
    """
    is_dry = dry_run if dry_run is not None else settings.DRY_RUN
    client = rest_client or get_rest_client()

    try:
        trade = await async_get_trade_by_id(trade_id)
    except Exception as exc:
        return OrderManagementResult(
            success=False, operation="cancel_order",
            failure_reason=f"db_error: {exc}", is_critical=False,
        )

    if trade is None:
        return OrderManagementResult(
            success=False, operation="cancel_order",
            failure_reason=f"trade_not_found: id={trade_id}", is_critical=False,
        )

    pair = trade["pair"]

    # Cari order_id: dari parameter, fallback ke fetch_open_orders
    oid = exchange_order_id
    if not oid:
        try:
            open_orders = await client.fetch_open_orders(pair)
            if open_orders:
                oid = _parse_order_id(open_orders[0])
        except Exception as exc:
            logger.warning(
                "[order_manager] Gagal fetch open orders untuk %s: %s", pair, exc
            )

    if not oid:
        # Tidak ada order yang bisa di-cancel — mungkin sudah fill
        logger.info(
            "[order_manager] Tidak ada open order ditemukan untuk %s trade_id=%d "
            "— dianggap sudah fill atau sudah di-cancel.",
            pair, trade_id,
        )
        await async_cancel_trade(trade_id)
        return OrderManagementResult(
            success=True, operation="cancel_order", pair=pair, trade_id=trade_id,
            cancelled_order_id=None, is_dry_run=is_dry,
            notes=["Tidak ada open order ditemukan — trade ditandai cancelled di DB."],
        )

    try:
        await _cancel_exchange_order(client, pair, oid, is_dry)
    except CriticalError as exc:
        msg = f"Critical error saat cancel order {pair}: {exc}"
        logger.error("[order_manager] %s", msg)
        await async_log_event(
            EventType.OTHER, msg, component=Component.ORDER_EXECUTION,
            severity=Severity.CRITICAL, trade_id=trade_id,
        )
        return OrderManagementResult(
            success=False, operation="cancel_order", pair=pair, trade_id=trade_id,
            failure_reason=_humanize_exchange_error(exc.original or exc), is_critical=True,
        )
    except TransientError as exc:
        msg = f"Transient error saat cancel order {pair}: {exc}"
        logger.warning("[order_manager] %s", msg)
        await async_log_event(
            EventType.OTHER, msg, component=Component.ORDER_EXECUTION,
            severity=Severity.WARNING, trade_id=trade_id,
        )
        return OrderManagementResult(
            success=False, operation="cancel_order", pair=pair, trade_id=trade_id,
            failure_reason=_humanize_exchange_error(exc.original or exc), is_critical=False,
        )

    # Update DB
    try:
        await async_cancel_trade(trade_id)
    except Exception as exc:
        logger.warning("[order_manager] DB cancel trade gagal (non-fatal): %s", exc)

    dry_tag = "[DRY-RUN] " if is_dry else ""
    msg = f"{dry_tag}Order cancelled: {pair} trade_id={trade_id} order_id={oid}"
    await async_log_event(
        EventType.OTHER, msg, component=Component.ORDER_EXECUTION,
        severity=Severity.INFO, trade_id=trade_id,
    )

    return OrderManagementResult(
        success=True, operation="cancel_order", pair=pair, trade_id=trade_id,
        cancelled_order_id=oid, is_dry_run=is_dry,
    )


# ── 3. Close Position ────────────────────────────────────────────────────────

async def _close_position_on_exchange(
    client: BitgetRestClient,
    symbol: str,
    direction: str,
    position_size: float,
    dry_run: bool,
) -> Dict[str, Any]:
    """
    Close posisi via market order (reduceOnly=True).
    Raise CriticalError / TransientError ke caller.
    """
    if dry_run:
        logger.info(
            "[order_manager][DRY-RUN] Would close %s %s size=%g by market",
            symbol, direction, position_size,
        )
        return {"id": "DRY_RUN_CLOSE", "symbol": symbol, "average": None}

    side = _opposite_side(direction)

    try:
        exchange = await client._get_exchange()
        raw = await exchange.create_market_order(
            symbol, side, position_size,
            params={
                "reduceOnly": True,
                "holdSide": _hold_side(direction),
                "marginMode": "cross",
                "productType": "USDT-FUTURES",
            },
        )
        logger.info(
            "[order_manager] Position closed: %s %s size=%g → order_id=%s",
            symbol, direction, position_size, _parse_order_id(raw),
        )
        return raw

    except (CriticalError, TransientError):
        raise
    except ccxt.InvalidOrder as exc:
        raise CriticalError(
            f"[order_manager] Close order ditolak {symbol}: {exc}", original=exc
        ) from exc
    except ccxt.InsufficientFunds as exc:
        raise CriticalError(
            f"[order_manager] Insufficient funds saat close {symbol}: {exc}",
            original=exc,
        ) from exc
    except ccxt.AuthenticationError as exc:
        raise CriticalError(
            f"[order_manager] Auth error saat close {symbol}: {exc}", original=exc
        ) from exc
    except ccxt.NetworkError as exc:
        raise TransientError(
            f"[order_manager] Network error saat close {symbol}: {exc}", original=exc
        ) from exc
    except ccxt.RequestTimeout as exc:
        raise TransientError(
            f"[order_manager] Timeout saat close {symbol}: {exc}", original=exc
        ) from exc
    except ccxt.RateLimitExceeded as exc:
        raise TransientError(
            f"[order_manager] Rate limit saat close {symbol}: {exc}", original=exc
        ) from exc
    except Exception as exc:
        raise CriticalError(
            f"[order_manager] Unexpected error saat close {symbol}: {exc}",
            original=exc,
        ) from exc


async def close_position(
    trade_id: int,
    *,
    close_reason: str = CloseReason.MANUAL,
    rest_client: Optional[BitgetRestClient] = None,
    dry_run: Optional[bool] = None,
) -> OrderManagementResult:
    """
    Close posisi open by market.

    Dipanggil oleh:
      - Command /close {pair} dari Telegram (Step 17)
      - Pipeline saat conflict_mode=replace dan ada posisi open lama
      - WebSocket handler saat SL hit terdeteksi (close_reason=CloseReason.SL_HIT)
      - WebSocket handler saat TP manual hit (close_reason=CloseReason.TP_HIT)
    """
    is_dry = dry_run if dry_run is not None else settings.DRY_RUN
    client = rest_client or get_rest_client()

    try:
        trade = await async_get_trade_by_id(trade_id)
    except Exception as exc:
        return OrderManagementResult(
            success=False, operation="close_position",
            failure_reason=f"db_error: {exc}", is_critical=False,
        )

    if trade is None:
        return OrderManagementResult(
            success=False, operation="close_position",
            failure_reason=f"trade_not_found: id={trade_id}", is_critical=False,
        )

    pair = trade["pair"]
    direction = trade.get("direction", Direction.LONG)
    position_size = _safe_float(trade.get("position_size"))

    if position_size <= 0:
        return OrderManagementResult(
            success=False, operation="close_position", pair=pair, trade_id=trade_id,
            failure_reason="invalid_position_size: position_size=0", is_critical=False,
        )

    try:
        raw = await _close_position_on_exchange(
            client, pair, direction, position_size, is_dry
        )
    except CriticalError as exc:
        msg = f"Critical error saat close posisi {pair}: {exc}"
        logger.error("[order_manager] %s", msg)
        await async_log_event(
            EventType.OTHER, msg, component=Component.ORDER_EXECUTION,
            severity=Severity.CRITICAL, trade_id=trade_id,
        )
        return OrderManagementResult(
            success=False, operation="close_position", pair=pair, trade_id=trade_id,
            failure_reason=_humanize_exchange_error(exc.original or exc), is_critical=True,
        )
    except TransientError as exc:
        msg = f"Transient error saat close posisi {pair}: {exc}"
        logger.warning("[order_manager] %s", msg)
        await async_log_event(
            EventType.OTHER, msg, component=Component.ORDER_EXECUTION,
            severity=Severity.WARNING, trade_id=trade_id,
        )
        return OrderManagementResult(
            success=False, operation="close_position", pair=pair, trade_id=trade_id,
            failure_reason=_humanize_exchange_error(exc.original or exc), is_critical=False,
        )

    # Estimasi PnL dari raw close order (tidak selalu tersedia)
    fill_price = _safe_float(raw.get("average") or raw.get("price"))
    entry_price = _safe_float(trade.get("entry_price"))
    size = _safe_float(trade.get("position_size"))
    pnl: Optional[float] = None
    if fill_price and entry_price and size:
        if direction == Direction.LONG:
            pnl = (fill_price - entry_price) * size
        else:
            pnl = (entry_price - fill_price) * size
        risk = _safe_float(trade.get("risk_amount_usd"), 1.0)
        r_multiple = round(pnl / risk, 2) if risk else None
    else:
        r_multiple = None

    # Update DB
    try:
        await async_close_trade(
            trade_id,
            close_reason=close_reason,
            pnl=round(pnl, 4) if pnl is not None else None,
            r_multiple=r_multiple,
        )
    except Exception as exc:
        logger.warning("[order_manager] DB close trade gagal (non-fatal): %s", exc)

    dry_tag = "[DRY-RUN] " if is_dry else ""
    pnl_str = f"{pnl:+.4f} USDT" if pnl is not None else "N/A"
    msg = (
        f"{dry_tag}Position closed: {pair} {direction.upper()} | "
        f"reason={close_reason} pnl≈{pnl_str} trade_id={trade_id}"
    )
    await async_log_event(
        EventType.OTHER, msg, component=Component.ORDER_EXECUTION,
        severity=Severity.INFO, trade_id=trade_id,
    )

    return OrderManagementResult(
        success=True, operation="close_position", pair=pair, trade_id=trade_id,
        closed_pnl=pnl, is_dry_run=is_dry,
    )


# ── 4. Close All Positions (emergency) ──────────────────────────────────────

async def close_all_positions(
    *,
    close_reason: str = CloseReason.MANUAL,
    rest_client: Optional[BitgetRestClient] = None,
    dry_run: Optional[bool] = None,
) -> OrderManagementResult:
    """
    Emergency close semua posisi open.

    Dipanggil oleh command /closeall dari Telegram (Step 17).
    Iterasi semua posisi open di DB, close satu per satu.
    Kegagalan satu posisi tidak menghentikan proses close posisi lain.
    Semua pair yang gagal dilaporkan di result.failed_pairs.

    Alur:
      1. Fetch semua trade open dari DB
      2. Jika ada posisi live di exchange yang tidak ada di DB, close juga
         (deteksi via fetch_positions)
      3. Close satu per satu, kumpulkan hasil
    """
    is_dry = dry_run if dry_run is not None else settings.DRY_RUN
    client = rest_client or get_rest_client()

    # Fetch semua trade open dari DB
    try:
        db_open_trades = await async_get_open_trades()
    except Exception as exc:
        return OrderManagementResult(
            success=False, operation="close_all",
            failure_reason=f"db_error: {exc}", is_critical=False,
        )

    # Fetch posisi live dari exchange — untuk deteksi posisi yang tidak di DB
    live_pairs_set: set = set()
    try:
        live_positions = await client.fetch_positions()
        for pos in live_positions:
            contracts = _safe_float(pos.get("contracts") or pos.get("contractSize"))
            if contracts > 0:
                live_pairs_set.add(pos.get("symbol", ""))
    except Exception as exc:
        logger.warning(
            "[order_manager] close_all: gagal fetch live positions: %s "
            "— lanjut close berdasar DB saja",
            exc,
        )

    # Bangun daftar final yang harus di-close
    trades_to_close = list(db_open_trades)

    # Tambahkan posisi live yang tidak ada di DB (posisi manual)
    db_pairs = {t["pair"] for t in db_open_trades}
    untracked_live = live_pairs_set - db_pairs
    for up in untracked_live:
        # Buat pseudo-trade entry untuk close (tidak ada trade_id DB)
        trades_to_close.append({"id": None, "pair": up, "_untracked": True})

    if not trades_to_close:
        return OrderManagementResult(
            success=True, operation="close_all", is_dry_run=is_dry,
            notes=["Tidak ada posisi open yang perlu di-close."],
        )

    closed_pairs: List[str] = []
    failed_pairs: List[str] = []

    for trade in trades_to_close:
        pair = trade["pair"]
        trade_id = trade.get("id")
        is_untracked = trade.get("_untracked", False)

        if is_untracked:
            # Posisi tidak ada di DB — fetch live info untuk close
            try:
                live_list = await client.fetch_positions([pair])
                direction = Direction.LONG
                size = 0.0
                for pos in live_list:
                    if pos.get("symbol") == pair:
                        size = _safe_float(pos.get("contracts") or pos.get("contractSize"))
                        side = (pos.get("side") or "").lower()
                        direction = Direction.SHORT if side == "short" else Direction.LONG
                        break

                if size <= 0:
                    continue  # Posisi sudah hilang

                raw = await _close_position_on_exchange(client, pair, direction, size, is_dry)
                closed_pairs.append(pair)
                logger.info(
                    "[order_manager] close_all: closed untracked %s dir=%s size=%g",
                    pair, direction, size,
                )
            except Exception as exc:
                logger.error(
                    "[order_manager] close_all: gagal close untracked %s: %s", pair, exc
                )
                failed_pairs.append(pair)
            continue

        # Close via trade_id DB
        result = await close_position(
            trade_id,
            close_reason=close_reason,
            rest_client=client,
            dry_run=is_dry,
        )
        if result.success:
            closed_pairs.append(pair)
        else:
            failed_pairs.append(pair)
            logger.error(
                "[order_manager] close_all: gagal close %s trade_id=%s: %s",
                pair, trade_id, result.failure_reason,
            )

    overall_success = len(failed_pairs) == 0
    dry_tag = "[DRY-RUN] " if is_dry else ""
    msg = (
        f"{dry_tag}close_all: closed={closed_pairs} failed={failed_pairs} "
        f"reason={close_reason}"
    )
    await async_log_event(
        EventType.OTHER, msg, component=Component.ORDER_EXECUTION,
        severity=Severity.INFO if overall_success else Severity.WARNING,
    )

    return OrderManagementResult(
        success=overall_success,
        operation="close_all",
        closed_pairs=closed_pairs,
        failed_pairs=failed_pairs,
        is_dry_run=is_dry,
        failure_reason=(
            f"Beberapa posisi gagal di-close: {failed_pairs}"
            if failed_pairs else None
        ),
    )


# ── Notifikasi ───────────────────────────────────────────────────────────────

def format_order_management_notification(result: OrderManagementResult) -> str:
    dry_tag = "🔵 [DRY-RUN] " if result.is_dry_run else ""

    if result.operation == "close_all":
        header = (
            f"{dry_tag}⚠️ Close All selesai (sebagian gagal)"
            if result.failed_pairs
            else f"{dry_tag}✅ Close All selesai"
        )
        lines = [header]
        if result.closed_pairs:
            lines.append(f"• Closed: {', '.join(result.closed_pairs)}")
        if result.failed_pairs:
            lines.append(f"❌ Gagal: {', '.join(result.failed_pairs)}")
        if result.notes:
            lines += [f"ℹ️ {n}" for n in result.notes]
        return "\n".join(lines)

    if not result.success:
        critical_tag = "🔴 CRITICAL" if result.is_critical else "⚠️"
        return (
            f"{critical_tag} Operasi {result.operation} gagal"
            + (f" [{result.pair}]" if result.pair else "")
            + f"\nAlasan: {result.failure_reason}"
        )

    if result.operation == "set_sl":
        sl_id = f" (id={result.sl_order_id})" if result.sl_order_id else ""
        return (
            f"{dry_tag}🛑 Stop Loss set: {result.pair}\n"
            f"• Trigger @ {result.sl_price:g}{sl_id}\n"
            f"• Trade ID: {result.trade_id}"
        )

    if result.operation == "cancel_order":
        oid = f" (id={result.cancelled_order_id})" if result.cancelled_order_id else ""
        lines = [f"{dry_tag}❌ Order cancelled: {result.pair}{oid}"]
        if result.notes:
            lines += [f"ℹ️ {n}" for n in result.notes]
        return "\n".join(lines)

    if result.operation == "close_position":
        pnl_str = (
            f"{result.closed_pnl:+.4f} USDT" if result.closed_pnl is not None else "N/A"
        )
        return (
            f"{dry_tag}✅ Posisi closed: {result.pair}\n"
            f"• PnL estimasi: {pnl_str}\n"
            f"• Trade ID: {result.trade_id}"
        )

    if result.operation == "close_all":
        header = (
            f"{dry_tag}⚠️ Close All selesai (sebagian gagal)"
            if result.failed_pairs
            else f"{dry_tag}✅ Close All selesai"
        )
        lines = [header]
        if result.closed_pairs:
            lines.append(f"• Closed: {', '.join(result.closed_pairs)}")
        if result.failed_pairs:
            lines.append(f"❌ Gagal: {', '.join(result.failed_pairs)}")
        if result.notes:
            lines += [f"ℹ️ {n}" for n in result.notes]
        return "\n".join(lines)

    return f"{dry_tag}✅ Operasi {result.operation} selesai."