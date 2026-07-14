"""
bot/executor/open_position.py
==============================
Step 12 — Bitget executor: open position.

Tanggung jawab:
  1. Set margin mode CROSS untuk simbol target (wajib sebelum entry)
  2. Set leverage hasil Step 10 (leverage_safe dari LeverageSafetyResult)
  3. Eksekusi order market atau limit ke Bitget Futures
  4. Dry-run mode: log semua langkah tanpa kirim order real ke exchange
  5. Klasifikasi error: transient (retry oleh @with_retry) vs critical
     (trip circuit breaker — di-raise ke caller, Step 19/circuit breaker Step 14)
  6. Log semua eksekusi ke database (tabel trades + event_log)

Stop Loss & Take Profit:
  - MARKET dan LIMIT order SAMA-SAMA attach SL (+ TP default RR1:2, dihitung
    dari sl_distance) secara atomic ke request order itu sendiri, lewat
    parameter unified ccxt `stopLoss={"triggerPrice": ...}` dan
    `takeProfit={"triggerPrice": ...}` (di-map ke `presetStopLossPrice` /
    `presetStopSurplusPrice` Bitget — preset yang nempel ke order INI, aktif
    otomatis begitu order fill, market maupun limit).
  - sl_price WAJIB ada untuk KEDUA entry_type — kalau kosong, open_position()
    menolak sebelum order dikirim ke exchange. Tidak ada lagi window "posisi
    live tanpa SL" untuk market order — dulu SL market dipasang terpisah
    setelah fill (reduceOnly stop_market, ada delay/celah), sekarang nempel
    bareng entry sama seperti limit.
  - tp_price dihitung SEKALI sebelum order dikirim (referensi harga: harga
    limit untuk LIMIT, `risk.entry_price_used`/ticker untuk MARKET — estimasi,
    bisa beda tipis dari fill price aktual tapi cukup untuk level TP absolut).
    Kalau gagal dihitung, order tetap jalan tanpa TP attached — fallback ke
    /settp manual.
  - order_manager.set_stop_loss()/set_take_profit() (Step 13) TIDAK lagi
    bagian dari alur open otomatis — sekarang murni dipakai command manual
    /setsl /settp untuk update posisi yang sudah open.

    PENTING — jangan pakai params.stopLossPrice/takeProfitPrice (tanpa
    "preset") di sini: ccxt.bitget menafsirkan itu sebagai *trigger order
    terpisah* (planType=pos_loss/pos_profit) yang butuh posisi yang SUDAH ADA
    di exchange untuk di-attach via holdSide — order yang belum fill tidak
    punya posisi sama sekali, jadi request itu selalu ditolak Bitget dengan
    error 43011 "holdSide error". `stopLoss`/`takeProfit={"triggerPrice": ...}`
    adalah jalur yang benar: ccxt me-map ke preset field, nempel ke order
    entry itu sendiri (tidak butuh holdSide/posisi).

Scope yang TIDAK dikerjakan di sini:
  - Cancel pending order / close posisi → Step 13
  - Circuit breaker state machine       → Step 14
  - Inline confirmation & timeout       → Step 18

Input utama (dari pipeline Step 19):
    result = await open_position(
        signal=signal,              # ParsedSignal (Step 4/5)
        risk=risk_result,           # RiskCalculationResult (Step 9)
        safety=safety_result,       # LeverageSafetyResult (Step 10)
        conflict_action=action,     # PositionAction.* (Step 11)
        rest_client=client,
        dry_run=settings.DRY_RUN,
    )

Output:
    ExecutionResult — success flag, trade_id, order_id, notif text,
    atau failure_reason jika gagal.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import ccxt

from bot.leverage_engine.leverage_engine import LeverageSafetyResult
from bot.parser.signal_parser import ParsedSignal
from bot.risk_engine.risk_engine import RiskCalculationResult, calculate_default_tp_price
from config.settings import settings
from core.constants import (
    Component,
    Direction,
    EntryType,
    EventType,
    Severity,
    TradeStatus,
)
from core.logging_setup import get_logger
from db.crud.event_log import async_log_event
from db.crud.trades import async_create_trade, async_update_trade_status
from exchange.bitget.rest_client import BitgetRestClient, get_rest_client
from exchange.bitget.retry import CriticalError, TransientError, with_retry

logger = get_logger(__name__)


# ── Result dataclass ────────────────────────────────────────────────────────

@dataclass
class ExecutionResult:
    """
    Hasil eksekusi open position — dikembalikan ke pipeline (Step 19).

    Jika success=True:
      - trade_id  : id record di tabel trades
      - order_id  : id order dari Bitget (None kalau dry_run)
      - is_dry_run: True kalau eksekusi tidak benar-benar dikirim ke exchange

    Jika success=False:
      - failure_reason berisi kategori error
      - is_critical=True → caller harus trip circuit breaker ORDER_EXECUTION
    """
    success: bool
    pair: str

    trade_id: Optional[int] = None
    order_id: Optional[str] = None
    is_dry_run: bool = False
    entry_type: Optional[str] = None
    direction: Optional[str] = None

    # Leverage yang dipakai (setelah safety adjustment Step 10)
    leverage_used: Optional[float] = None
    leverage_adjusted: bool = False

    # Ringkasan order untuk notifikasi Telegram
    entry_price_actual: Optional[float] = None   # fill price (market) atau limit price
    position_size: Optional[float] = None
    margin_used: Optional[float] = None

    # Proteksi + risk metrics — buat notifikasi institutional-style
    sl_price: Optional[float] = None
    tp_price: Optional[float] = None
    risk_amount_usd: Optional[float] = None
    risk_percent_used: Optional[float] = None

    failure_reason: Optional[str] = None
    is_critical: bool = False                    # True → trip circuit breaker
    notes: list = field(default_factory=list)

    def notification_text(self) -> str:
        return format_execution_notification(self)


# ── Helper ──────────────────────────────────────────────────────────────────

def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _utcnow_display() -> str:
    """Timestamp ringkas buat footer notifikasi: 'YYYY-MM-DD HH:MM:SS UTC'."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _ccxt_side(direction: str) -> str:
    """Konversi Direction.* ke ccxt side string ('buy'/'sell')."""
    return "buy" if direction == Direction.LONG else "sell"


def _to_int_leverage(leverage: float) -> int:
    """Floor leverage ke integer — ccxt.bitget butuh integer."""
    return max(1, math.floor(leverage))


def _parse_order_id(raw_order: Dict[str, Any]) -> str:
    return str(
        raw_order.get("id")
        or raw_order.get("orderId")
        or raw_order.get("info", {}).get("orderId")
        or ""
    )


def _parse_fill_price(raw_order: Dict[str, Any], fallback: float) -> float:
    """
    Ambil harga fill aktual dari raw order ccxt.
    Market order: pakai 'average' atau 'price'.
    Limit order: pakai 'price' (belum fill saat ini, entry_price adalah harga limit).
    """
    avg = raw_order.get("average")
    price = raw_order.get("price")
    if avg and float(avg) > 0:
        return float(avg)
    if price and float(price) > 0:
        return float(price)
    return fallback


# ── Setup pre-order: margin mode + leverage ─────────────────────────────────

async def _setup_margin_and_leverage(
    client: BitgetRestClient,
    symbol: str,
    leverage: int,
    dry_run: bool,
) -> None:
    """
    Langkah wajib sebelum buka posisi manapun:
      1. Set margin mode CROSS (bagian 4.2 prompt.md — keputusan final)
      2. Set leverage ke angka yang sudah divalidasi Step 10

    Dry-run: log aksi tanpa kirim ke exchange.
    Raise CriticalError / TransientError ke caller (open_position).
    """
    if dry_run:
        logger.info(
            "[executor][DRY-RUN] Would set_cross_margin(%s) + set_leverage(%s, %dx)",
            symbol, symbol, leverage,
        )
        return

    await client.set_cross_margin(symbol)
    await client.set_leverage(symbol, leverage)
    logger.info(
        "[executor] margin=cross + leverage=%dx set untuk %s", leverage, symbol
    )


# ── Eksekusi order ──────────────────────────────────────────────────────────

async def _place_order(
    client: BitgetRestClient,
    symbol: str,
    direction: str,
    entry_type: str,
    position_size: float,
    entry_price: Optional[float],
    sl_price: Optional[float],
    tp_price: Optional[float],
    dry_run: bool,
) -> Dict[str, Any]:
    """
    Kirim order ke Bitget Futures via ccxt.

    MARKET & LIMIT SAMA-SAMA attach SL (+ TP kalau berhasil dihitung) secara
    atomic ke request order itu sendiri, lewat parameter unified ccxt
    `stopLoss={"triggerPrice": ...}` / `takeProfit={"triggerPrice": ...}`
    (di-map ke `presetStopLossPrice` / `presetStopSurplusPrice` Bitget).
    Preset ini bukan trigger order terpisah — dia nempel ke order entry dan
    aktif otomatis begitu order fill, TIDAK butuh holdSide/posisi yang sudah
    ada. Ini berlaku sama buat market maupun limit (dikonfirmasi dari
    createOrderRequest ccxt.bitget — branch preset dipakai untuk order type
    apapun selama bukan trigger/plan order).

    Konsekuensi: sejak sekarang TIDAK ADA window "posisi live tanpa SL" buat
    market order — SL nempel bareng fill, bukan dipasang terpisah setelah
    fill lewat order_manager.set_stop_loss (fungsi itu sekarang cuma dipakai
    /setsl manual, bukan bagian alur open otomatis lagi — lihat
    signal_pipeline.py).

    sl_price WAJIB (>0) untuk KEDUA entry_type — kalau kosong, order ditolak
    di sini sebelum sempat dikirim ke exchange tanpa proteksi.
    tp_price OPSIONAL — kalau None (gagal dihitung di caller), order tetap
    jalan tanpa TP attached, notifikasi caller yang urus fallback /settp.

    Dry-run: return stub dict tanpa menyentuh exchange.
    Raise CriticalError / TransientError ke caller.
    """
    side = _ccxt_side(direction)

    if sl_price is None or sl_price <= 0:
        raise CriticalError(
            f"[executor] {entry_type.upper()} order untuk {symbol} tapi sl_price "
            f"kosong/invalid ({sl_price}) — SL WAJIB di-attach bareng saat order "
            "dikirim (market maupun limit), parser/risk engine seharusnya sudah "
            "menangkap ini.",
        )

    if dry_run:
        logger.info(
            "[executor][DRY-RUN] Would place %s %s %s @ %s | size=%g | "
            "SL(attached)=%g%s",
            entry_type.upper(), symbol, side,
            entry_price if entry_type == EntryType.LIMIT else "market",
            position_size, sl_price,
            f" | TP(attached)={tp_price:g}" if tp_price else " | TP=gagal dihitung",
        )
        return {
            "id": "DRY_RUN",
            "symbol": symbol,
            "side": side,
            "type": entry_type,
            "amount": position_size,
            "price": entry_price,
            "average": entry_price,
            "status": "open" if entry_type == EntryType.LIMIT else "closed",
        }

    try:
        exchange = await client._get_exchange()

        # sl_price sudah divalidasi wajib ada di atas (guard sebelum dry_run).
        # `stopLoss={"triggerPrice": ...}` / `takeProfit={"triggerPrice": ...}`
        # (unified ccxt) = preset SL/TP Bitget (presetStopLossPrice /
        # presetStopSurplusPrice) — nempel ke order INI, aktif otomatis begitu
        # order fill, TANPA butuh posisi yang sudah ada. Berlaku sama untuk
        # market maupun limit.
        #
        # JANGAN pakai params.stopLossPrice/takeProfitPrice (tanpa "preset") —
        # ccxt.bitget menafsirkannya sebagai trigger order TERPISAH
        # (planType=pos_loss/pos_profit) yang butuh holdSide + posisi yang
        # SUDAH ADA di exchange. Order yang belum fill tidak punya posisi,
        # jadi request itu selalu ditolak Bitget: error 43011 "holdSide error".
        attach_params: Dict[str, Any] = {
            "marginMode": "cross",
            "productType": "USDT-FUTURES",
            "stopLoss": {"triggerPrice": sl_price},
        }
        if tp_price and tp_price > 0:
            attach_params["takeProfit"] = {"triggerPrice": tp_price}

        if entry_type == EntryType.MARKET:
            raw = await exchange.create_market_order(
                symbol, side, position_size, params=attach_params,
            )
        else:
            # LIMIT order — entry_price wajib ada (validated upstream)
            if entry_price is None:
                raise CriticalError(
                    f"[executor] Limit order untuk {symbol} tapi entry_price=None — "
                    "parser/risk engine seharusnya sudah menangkap ini.",
                )
            raw = await exchange.create_limit_order(
                symbol, side, position_size, entry_price, params=attach_params,
            )

        logger.info(
            "[executor] Order placed: %s %s %s @ %s → id=%s",
            entry_type.upper(), symbol, side, entry_price or "market",
            _parse_order_id(raw),
        )
        return raw

    except CriticalError:
        raise
    except ccxt.InsufficientFunds as exc:
        raise CriticalError(
            f"[executor] Insufficient funds untuk {symbol}: {exc}", original=exc
        ) from exc
    except ccxt.InvalidOrder as exc:
        raise CriticalError(
            f"[executor] Order ditolak exchange (invalid): {symbol} — {exc}",
            original=exc,
        ) from exc
    except ccxt.BadSymbol as exc:
        raise CriticalError(
            f"[executor] Simbol {symbol} tidak dikenali Bitget: {exc}", original=exc
        ) from exc
    except ccxt.AuthenticationError as exc:
        raise CriticalError(
            f"[executor] Auth error — API key tidak valid/expired: {exc}", original=exc
        ) from exc
    except ccxt.PermissionDenied as exc:
        raise CriticalError(
            f"[executor] Permission ditolak untuk {symbol}: {exc}", original=exc
        ) from exc
    except ccxt.NetworkError as exc:
        raise TransientError(
            f"[executor] Network error saat place order {symbol}: {exc}", original=exc
        ) from exc
    except ccxt.RequestTimeout as exc:
        raise TransientError(
            f"[executor] Timeout saat place order {symbol}: {exc}", original=exc
        ) from exc
    except ccxt.RateLimitExceeded as exc:
        raise TransientError(
            f"[executor] Rate limit exceeded: {exc}", original=exc
        ) from exc
    except Exception as exc:
        raise CriticalError(
            f"[executor] Unexpected error saat place order {symbol}: {exc}", original=exc
        ) from exc


# ── Log ke database ─────────────────────────────────────────────────────────

async def _record_trade(
    signal: ParsedSignal,
    risk: RiskCalculationResult,
    safety: LeverageSafetyResult,
    order_raw: Dict[str, Any],
    entry_price_actual: float,
    leverage_used: int,
    conflict_action: Optional[str],
    is_dry_run: bool,
    tp_price: Optional[float] = None,
) -> int:
    """
    Insert record trade baru ke database.
    Return trade_id yang baru dibuat.
    """
    entry_type = signal.entry_type or EntryType.MARKET
    status = (
        TradeStatus.PENDING
        if entry_type == EntryType.LIMIT
        else TradeStatus.OPEN
    )

    order_id_str = _parse_order_id(order_raw)

    sl_price_final = risk.sl_price or signal.stop_loss or 0.0

    # tp_price di sini adalah nilai yang SAMA dengan yang di-attach ke order
    # (dihitung sekali di open_position(), sebelum order dikirim) — bukan
    # dihitung ulang, supaya DB selalu konsisten dengan preset TP yang live
    # di exchange. None kalau caller gagal hitung (SL/entry invalid) — TP
    # nanti diset manual via /settp.
    tp_price_default = tp_price

    trade_id = await async_create_trade(
        pair=signal.pair_normalized or signal.pair_raw or "",
        direction=signal.direction or Direction.LONG,
        entry_type=entry_type,
        entry_price=entry_price_actual,
        sl_price=sl_price_final,
        tp_price=tp_price_default,
        position_size=risk.position_size or 0.0,
        margin_used=risk.margin_needed,
        risk_mode=risk.risk_mode,
        risk_amount_usd=risk.risk_amount_usd,
        risk_percent_used=risk.risk_percent_used,
        max_leverage_available=risk.max_leverage_available,
        leverage_used=float(leverage_used),
        leverage_auto_adjusted=safety.leverage_adjusted,
        liquidation_price_estimate=(
            safety.projection.liquidation_price if safety.projection else None
        ),
        status=status,
        opened_at=_utcnow() if status == TradeStatus.OPEN else None,
        raw_signal_text=signal.raw_text,
        source_analyst=None,
        source_message_id=None,
        conflict_action_taken=conflict_action,
    )

    return trade_id


# ── Fungsi utama ─────────────────────────────────────────────────────────────

async def open_position(
    signal: ParsedSignal,
    risk: RiskCalculationResult,
    safety: LeverageSafetyResult,
    *,
    conflict_action: Optional[str] = None,
    rest_client: Optional[BitgetRestClient] = None,
    dry_run: Optional[bool] = None,
) -> ExecutionResult:
    """
    Fungsi utama Step 12 — eksekusi open position ke Bitget Futures.

    Alur:
      1. Validasi input (pair, position_size, sl_price wajib ada)
      2. Set margin mode CROSS + set leverage (atau log saja kalau dry_run)
      3. Place order (market / limit) ke exchange
      4. Catat trade ke database
      5. Log event ke event_log
      6. Return ExecutionResult

    Error handling:
      - TransientError → caller (pipeline) bisa retry
      - CriticalError  → caller HARUS trip circuit breaker ORDER_EXECUTION
      - Semua error tetap di-log ke event_log sebelum di-raise
    """
    is_dry = dry_run if dry_run is not None else settings.DRY_RUN
    client = rest_client or get_rest_client()

    pair = signal.pair_normalized or signal.pair_raw or ""
    direction = signal.direction or Direction.LONG
    entry_type = signal.entry_type or EntryType.MARKET

    # ── Validasi input ──────────────────────────────────────────────────
    if not pair:
        return ExecutionResult(
            success=False,
            pair=pair,
            failure_reason="missing_pair",
            is_critical=False,
        )

    if not risk.success or risk.position_size is None or risk.position_size <= 0:
        return ExecutionResult(
            success=False,
            pair=pair,
            failure_reason=f"invalid_risk_result: {risk.failure_reason}",
            is_critical=False,
        )

    if not safety.success:
        return ExecutionResult(
            success=False,
            pair=pair,
            failure_reason=f"invalid_safety_result: {safety.failure_reason}",
            is_critical=False,
        )

    if entry_type == EntryType.LIMIT and (
        signal.entry_price is None or signal.entry_price <= 0
    ):
        return ExecutionResult(
            success=False,
            pair=pair,
            failure_reason="limit_order_missing_entry_price",
            is_critical=False,
        )

    sl_price = signal.stop_loss or risk.sl_price or 0.0
    if entry_type == EntryType.LIMIT and sl_price <= 0:
        # SL wajib dikirim BARENGAN order limit (attached via stopLossPrice) —
        # tidak ada mekanisme "set SL setelah fill" untuk limit order, jadi
        # kalau sl_price tidak ada, order limit ditolak di sini, sebelum
        # sempat dikirim ke exchange tanpa proteksi.
        return ExecutionResult(
            success=False,
            pair=pair,
            failure_reason="limit_order_missing_sl_price",
            is_critical=False,
        )
    if entry_type == EntryType.MARKET and sl_price <= 0:
        # Sama seperti limit — MARKET sekarang juga WAJIB attach SL+TP
        # atomically di request order (params.stopLoss/takeProfit, lihat
        # _place_order). Tanpa sl_price, tidak ada dasar hitung TP RR1:2,
        # dan order akan naked kalau tetap dikirim — tolak di sini.
        return ExecutionResult(
            success=False,
            pair=pair,
            failure_reason="market_order_missing_sl_price",
            is_critical=False,
        )

    leverage_used = _to_int_leverage(safety.leverage_safe)
    position_size = risk.position_size
    entry_price = signal.entry_price if entry_type == EntryType.LIMIT else None

    # ── TP default RR 1:2, dihitung SEKALI di sini, dipakai buat attach
    # atomically ke order (MARKET & LIMIT) DAN buat record DB (_record_trade)
    # — supaya angka TP yang nempel di exchange selalu sama dengan yang
    # tercatat di DB (satu sumber kebenaran, bukan dihitung dua kali).
    #
    # Referensi harga buat estimasi TP:
    #   - LIMIT  -> signal.entry_price (harga limit, exact)
    #   - MARKET -> risk.entry_price_used (ticker saat risk engine Step 9
    #     jalan - estimasi, bisa beda sedikit dari fill price aktual, tapi
    #     cukup buat hitung level TP absolut; tetap jauh lebih aman
    #     daripada tanpa TP sama sekali sampai fill event datang)
    tp_price_ref = entry_price if entry_type == EntryType.LIMIT else risk.entry_price_used
    tp_price: Optional[float] = None
    if tp_price_ref and tp_price_ref > 0 and sl_price > 0:
        try:
            tp_price = calculate_default_tp_price(
                direction, tp_price_ref, sl_price,
            )
        except ValueError as exc:
            logger.warning(
                "[executor] Gagal hitung default TP RR2 pre-order untuk %s: %s "
                "- order tetap jalan TANPA TP attached, set manual via /settp "
                "setelah fill.",
                pair, exc,
            )

    notes: list = []
    if safety.leverage_adjusted:
        notes.append(
            f"Leverage diturunkan otomatis: "
            f"{_to_int_leverage(safety.leverage_requested)}x → {leverage_used}x "
            f"(buffer liquidation)"
        )
    if safety.even_min_leverage_unsafe:
        notes.append(
            "⚠️ Bahkan leverage minimum pun proyeksi liquidation tidak aman — "
            "total exposure akun kemungkinan sudah sangat besar."
        )

    # ── Step 1: Setup margin mode + leverage ────────────────────────────
    try:
        await _setup_margin_and_leverage(client, pair, leverage_used, is_dry)
    except CriticalError as exc:
        msg = f"Gagal set margin/leverage untuk {pair}: {exc}"
        logger.error("[executor] %s", msg)
        await async_log_event(
            EventType.OTHER,
            msg,
            component=Component.ORDER_EXECUTION,
            severity=Severity.CRITICAL,
        )
        return ExecutionResult(
            success=False, pair=pair,
            failure_reason=f"setup_critical: {exc}",
            is_critical=True,
        )
    except TransientError as exc:
        msg = f"Transient error saat set margin/leverage untuk {pair}: {exc}"
        logger.warning("[executor] %s", msg)
        await async_log_event(
            EventType.OTHER, msg,
            component=Component.ORDER_EXECUTION, severity=Severity.WARNING,
        )
        return ExecutionResult(
            success=False, pair=pair,
            failure_reason=f"setup_transient: {exc}",
            is_critical=False,
        )

    # ── Step 2: Place order ─────────────────────────────────────────────
    try:
        order_raw = await _place_order(
            client, pair, direction, entry_type,
            position_size, entry_price, sl_price, tp_price, is_dry,
        )
    except CriticalError as exc:
        msg = f"Critical error saat place order {pair}: {exc}"
        logger.error("[executor] %s", msg)
        await async_log_event(
            EventType.OTHER, msg,
            component=Component.ORDER_EXECUTION, severity=Severity.CRITICAL,
        )
        return ExecutionResult(
            success=False, pair=pair,
            failure_reason=f"order_critical: {exc}",
            is_critical=True,
        )
    except TransientError as exc:
        msg = f"Transient error saat place order {pair}: {exc}"
        logger.warning("[executor] %s", msg)
        await async_log_event(
            EventType.OTHER, msg,
            component=Component.ORDER_EXECUTION, severity=Severity.WARNING,
        )
        return ExecutionResult(
            success=False, pair=pair,
            failure_reason=f"order_transient: {exc}",
            is_critical=False,
        )

    entry_price_actual = _parse_fill_price(
        order_raw, fallback=entry_price or risk.entry_price_used or 0.0
    )
    order_id = _parse_order_id(order_raw)

    # ── Step 3: Catat ke database ───────────────────────────────────────
    try:
        trade_id = await _record_trade(
            signal, risk, safety, order_raw,
            entry_price_actual, leverage_used,
            conflict_action, is_dry, tp_price,
        )
    except Exception as exc:
        # DB error bukan alasan batalkan trade yang sudah dikirim ke exchange
        msg = f"DB error setelah order {pair} berhasil: {exc}"
        logger.error("[executor] %s", msg)
        await async_log_event(
            EventType.OTHER, msg,
            component=Component.ORDER_EXECUTION, severity=Severity.WARNING,
        )
        trade_id = None

    # ── Step 4: Log event sukses ────────────────────────────────────────
    dry_tag = "[DRY-RUN] " if is_dry else ""
    entry_label = (
        f"limit @ {entry_price_actual:g}"
        if entry_type == EntryType.LIMIT
        else f"market @ ~{entry_price_actual:g}"
    )
    event_msg = (
        f"{dry_tag}Order placed: {pair} {direction.upper()} {entry_label} | "
        f"size={position_size:g} | margin≈{risk.margin_needed:.2f} USDT | "
        f"risk={risk.risk_amount_usd:.2f} USDT | leverage={leverage_used}x"
    )
    await async_log_event(
        EventType.OTHER, event_msg,
        component=Component.ORDER_EXECUTION, severity=Severity.INFO,
        trade_id=trade_id,
    )

    result = ExecutionResult(
        success=True,
        pair=pair,
        trade_id=trade_id,
        order_id=order_id if order_id and order_id != "DRY_RUN" else None,
        is_dry_run=is_dry,
        entry_type=entry_type,
        direction=direction,
        leverage_used=float(leverage_used),
        leverage_adjusted=safety.leverage_adjusted,
        entry_price_actual=entry_price_actual,
        position_size=position_size,
        margin_used=risk.margin_needed,
        sl_price=sl_price,
        tp_price=tp_price,
        risk_amount_usd=risk.risk_amount_usd,
        risk_percent_used=risk.risk_percent_used,
        notes=notes,
    )

    logger.info(
        "[executor] %s%s %s %s → trade_id=%s order_id=%s",
        "[DRY-RUN] " if is_dry else "",
        pair, direction.upper(), entry_type.upper(),
        trade_id, order_id or "N/A",
    )

    return result


# ── Notifikasi (institutional-style) ──────────────────────────────────────

_DIVIDER = "▬" * 22


def _pct_distance(reference: float, target: float) -> str:
    """% jarak target dari reference, buat display SL/TP distance."""
    if reference == 0:
        return "n/a"
    pct = (target - reference) / reference * 100
    return f"{pct:+.2f}%"


def _fmt_num(value: Optional[float], decimals: int = 2) -> str:
    if value is None:
        return "—"
    return f"{value:,.{decimals}f}"


def _fmt_price(value: Optional[float]) -> str:
    if value is None:
        return "—"
    return f"{value:g}"


def _risk_reward_ratio(entry: float, sl: float, tp: Optional[float]) -> str:
    if tp is None:
        return "—"
    sl_dist = abs(entry - sl)
    tp_dist = abs(tp - entry)
    if sl_dist == 0:
        return "—"
    return f"1 : {tp_dist / sl_dist:.2f}"


def format_execution_notification(result: ExecutionResult) -> str:
    """
    Notifikasi eksekusi entry — format institutional/desk-style: header
    status, blok proteksi (entry/SL/TP/R:R), blok sizing (size/margin/
    leverage/risk), footer (trade id + timestamp UTC). HTML parse mode
    (Telegram) — monospace lewat <code> buat angka biar sejajar/gampang
    dibaca cepat, konsisten dengan gaya notifikasi desk trading.
    """
    if not result.success:
        header = "🔴 <b>ENTRY REJECTED — CRITICAL</b>" if result.is_critical else "🟠 <b>ENTRY REJECTED</b>"
        return (
            f"{header}\n"
            f"{_DIVIDER}\n"
            f"<b>Pair</b>      <code>{result.pair or '—'}</code>\n"
            f"<b>Reason</b>    {result.failure_reason or 'unknown'}\n"
            f"{_DIVIDER}\n"
            f"<i>{_utcnow_display()}</i>"
        )

    is_long = result.direction == Direction.LONG
    side_badge = "🟢 LONG" if is_long else "🔴 SHORT"
    type_badge = "MKT" if result.entry_type == EntryType.MARKET else "LMT"
    dry_prefix = "🔵 <b>[SIMULATION]</b> " if result.is_dry_run else ""

    entry = result.entry_price_actual or 0.0
    sl = result.sl_price
    tp = result.tp_price

    lines = [
        f"{dry_prefix}✅ <b>POSITION OPENED</b>  <code>{result.pair}</code>",
        f"<b>{side_badge}</b>  ·  {type_badge}  ·  Cross {int(result.leverage_used)}x" if result.leverage_used else f"<b>{side_badge}</b>  ·  {type_badge}",
        _DIVIDER,
        f"<b>Entry</b>        <code>{_fmt_price(entry)}</code>",
    ]

    if sl:
        lines.append(
            f"<b>Stop Loss</b>   <code>{_fmt_price(sl)}</code>  "
            f"({_pct_distance(entry, sl)})"
        )
    if tp:
        lines.append(
            f"<b>Take Profit</b> <code>{_fmt_price(tp)}</code>  "
            f"({_pct_distance(entry, tp)})"
        )
    if sl:
        lines.append(f"<b>R : R</b>        {_risk_reward_ratio(entry, sl, tp)}")

    lines.append(_DIVIDER)
    lines.append(f"<b>Size</b>         <code>{_fmt_price(result.position_size)}</code>")
    if result.margin_used is not None:
        lines.append(f"<b>Margin</b>       <code>{_fmt_num(result.margin_used)} USDT</code>")
    if result.risk_amount_usd is not None:
        risk_pct = (
            f" ({result.risk_percent_used:.2f}%)"
            if result.risk_percent_used is not None
            else ""
        )
        lines.append(f"<b>Risk</b>         <code>{_fmt_num(result.risk_amount_usd)} USDT</code>{risk_pct}")

    if result.leverage_adjusted:
        lines.append(_DIVIDER)
        lines.append("📉 <i>Leverage auto-adjusted (liquidation buffer)</i>")
    for note in result.notes:
        if "Leverage diturunkan" in note:
            continue  # sudah direpresentasikan di baris leverage_adjusted di atas
        lines.append(f"ℹ️ <i>{note}</i>")

    lines.append(_DIVIDER)
    footer = f"Trade #{result.trade_id}" if result.trade_id else "Trade #—"
    footer += f" · {result.order_id}" if result.order_id else ""
    lines.append(f"<code>{footer}</code>")
    lines.append(f"<i>{_utcnow_display()}</i>")

    return "\n".join(lines)