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

Stop Loss:
  - MARKET order: fill instan → SL dipasang terpisah setelah fill via
    reduceOnly stop_market order (Step 13, order_manager.set_stop_loss).
  - LIMIT order: belum fill saat order dikirim, jadi TIDAK bisa pakai
    reduceOnly (butuh posisi yang sudah ada). SL untuk limit order WAJIB
    di-attach atomically ke order entry itu sendiri lewat parameter unified
    ccxt `stopLoss={"triggerPrice": ...}` (di-map ke `presetStopLossPrice`
    Bitget — preset SL yang nempel ke order INI, aktif otomatis begitu limit
    order fill). sl_price WAJIB ada untuk limit order — kalau kosong,
    open_position() menolak sebelum order dikirim ke exchange.

    PENTING — jangan pakai params.stopLossPrice (tanpa "preset") di sini:
    ccxt.bitget menafsirkan stopLossPrice sebagai *stop-loss TRIGGER order*
    (planType=pos_loss) yang butuh posisi yang SUDAH ADA di exchange untuk
    di-attach via holdSide — order limit entry yang belum fill tidak punya
    posisi sama sekali, jadi request itu selalu ditolak Bitget dengan error
    43011 "holdSide error". `stopLoss={"triggerPrice": ...}` adalah jalur
    yang benar: ccxt me-map ke `presetStopLossPrice`, preset SL yang
    ditempel ke order entry itu sendiri (tidak butuh holdSide/posisi).

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
from bot.risk_engine.risk_engine import RiskCalculationResult
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

    # Leverage yang dipakai (setelah safety adjustment Step 10)
    leverage_used: Optional[float] = None
    leverage_adjusted: bool = False

    # Ringkasan order untuk notifikasi Telegram
    entry_price_actual: Optional[float] = None   # fill price (market) atau limit price
    position_size: Optional[float] = None
    margin_used: Optional[float] = None

    failure_reason: Optional[str] = None
    is_critical: bool = False                    # True → trip circuit breaker
    notes: list = field(default_factory=list)

    def notification_text(self) -> str:
        return format_execution_notification(self)


# ── Helper ──────────────────────────────────────────────────────────────────

def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    dry_run: bool,
) -> Dict[str, Any]:
    """
    Kirim order ke Bitget Futures via ccxt.

    LIMIT order: sl_price WAJIB (>0) — di-attach ke request lewat parameter
    unified `stopLoss` (preset SL, bukan trigger order terpisah) supaya SL
    aktif otomatis begitu order fill (posisi belum ada saat order dikirim,
    jadi reduceOnly stop order terpisah tidak bisa dipakai di sini — lihat
    docstring modul).

    Dry-run: return stub dict tanpa menyentuh exchange.
    Raise CriticalError / TransientError ke caller.
    """
    side = _ccxt_side(direction)

    if entry_type == EntryType.LIMIT and (sl_price is None or sl_price <= 0):
        raise CriticalError(
            f"[executor] Limit order untuk {symbol} tapi sl_price kosong/invalid "
            f"({sl_price}) — SL WAJIB di-set bareng saat limit order dikirim, "
            "parser/risk engine seharusnya sudah menangkap ini.",
        )

    if dry_run:
        logger.info(
            "[executor][DRY-RUN] Would place %s %s %s @ %s | size=%g%s",
            entry_type.upper(), symbol, side,
            entry_price if entry_type == EntryType.LIMIT else "market",
            position_size,
            f" | SL(attached)={sl_price:g}" if entry_type == EntryType.LIMIT else "",
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

        if entry_type == EntryType.MARKET:
            raw = await exchange.create_market_order(
                symbol, side, position_size,
                params={"marginMode": "cross", "productType": "USDT-FUTURES"},
            )
        else:
            # LIMIT order — entry_price wajib ada (validated upstream)
            if entry_price is None:
                raise CriticalError(
                    f"[executor] Limit order untuk {symbol} tapi entry_price=None — "
                    "parser/risk engine seharusnya sudah menangkap ini.",
                )
            # sl_price sudah divalidasi wajib ada di atas (guard sebelum dry_run).
            # `stopLoss={"triggerPrice": ...}` (unified ccxt) = preset SL Bitget
            # (presetStopLossPrice) — nempel ke order INI, aktif otomatis begitu
            # limit order fill, TANPA butuh posisi yang sudah ada.
            #
            # JANGAN pakai params.stopLossPrice (tanpa "preset") — ccxt.bitget
            # menafsirkannya sebagai stop-loss TRIGGER order (planType=pos_loss)
            # yang butuh holdSide + posisi yang SUDAH ADA di exchange. Order
            # limit entry yang belum fill tidak punya posisi, jadi request itu
            # selalu ditolak Bitget: error 43011 "holdSide error".
            raw = await exchange.create_limit_order(
                symbol, side, position_size, entry_price,
                params={
                    "marginMode": "cross",
                    "productType": "USDT-FUTURES",
                    "stopLoss": {"triggerPrice": sl_price},
                },
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

    trade_id = await async_create_trade(
        pair=signal.pair_normalized or signal.pair_raw or "",
        direction=signal.direction or Direction.LONG,
        entry_type=entry_type,
        entry_price=entry_price_actual,
        sl_price=risk.sl_price or signal.stop_loss or 0.0,
        tp_price=None,
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

    if entry_type == EntryType.LIMIT and signal.entry_price is None:
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

    leverage_used = _to_int_leverage(safety.leverage_safe)
    position_size = risk.position_size
    entry_price = signal.entry_price if entry_type == EntryType.LIMIT else None

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
            position_size, entry_price, sl_price, is_dry,
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
            conflict_action, is_dry,
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
        leverage_used=float(leverage_used),
        leverage_adjusted=safety.leverage_adjusted,
        entry_price_actual=entry_price_actual,
        position_size=position_size,
        margin_used=risk.margin_needed,
        notes=notes,
    )

    logger.info(
        "[executor] %s%s %s %s → trade_id=%s order_id=%s",
        "[DRY-RUN] " if is_dry else "",
        pair, direction.upper(), entry_type.upper(),
        trade_id, order_id or "N/A",
    )

    return result


# ── Notifikasi ───────────────────────────────────────────────────────────────

def format_execution_notification(result: ExecutionResult) -> str:
    """Format teks notifikasi Telegram untuk hasil eksekusi open position."""
    if not result.success:
        critical_tag = "🔴 CRITICAL" if result.is_critical else "⚠️"
        return (
            f"{critical_tag} Gagal buka posisi {result.pair}\n"
            f"Alasan: {result.failure_reason}"
        )

    dry_tag = "🔵 [DRY-RUN] " if result.is_dry_run else ""
    entry_type_label = (
        "⏳ Limit order" if result.order_id or result.is_dry_run else "🚀 Market order"
    )
    lines = [
        f"{dry_tag}✅ {entry_type_label} dikirim: {result.pair}",
        f"• Entry  : {result.entry_price_actual:g}" if result.entry_price_actual else "",
        f"• Size   : {result.position_size:g}" if result.position_size else "",
        f"• Margin : ~{result.margin_used:.2f} USDT" if result.margin_used else "",
        f"• Leverage: {int(result.leverage_used)}x" if result.leverage_used else "",
    ]
    if result.leverage_adjusted:
        lines.append("📉 Leverage diturunkan otomatis (buffer liquidation)")
    for note in result.notes:
        lines.append(f"ℹ️ {note}")
    if result.trade_id:
        lines.append(f"🗂️ Trade ID: {result.trade_id}")

    return "\n".join(l for l in lines if l)