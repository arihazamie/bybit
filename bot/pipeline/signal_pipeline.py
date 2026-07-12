"""
bot/pipeline/signal_pipeline.py
=================================
Step 19 — Integrasi penuh: Telethon listener → parser → risk engine →
leverage safety → position checker → executor → circuit breaker → notifikasi.

Entry point utama: `SignalPipeline.process_raw_event(raw_event)`

Alur nominal:
  raw_event (dict dari listener)
    ↓ deduplication (signal_log)
    ↓ evaluate_signal() → SignalEvaluation
      INFO_ONLY  → log + notify
      AMBIGUOUS  → send_ambiguous_confirm (inline button Step 18)
      SUCCESS    ↓
    ↓ cek bot paused / circuit breaker OPEN
    ↓ check_position_condition(pair)
      SKIP       → log + notify
      ASK_CONF   → send_conflict_confirm (inline button Step 18)
      PROCEED/ADD/REPLACE ↓
    ↓ calculate_trade_risk()    [risk engine Step 9]
    ↓ run_leverage_safety_check() [leverage engine Step 10]
    ↓ open_position()           [executor Step 12 — SL wajib attached di request
                                  kalau LIMIT (params.stopLossPrice), sl_price
                                  kosong = order ditolak sebelum kirim ke exchange]
    ↓ set_stop_loss()            [executor Step 13 — HANYA untuk MARKET order
                                  (fill instan → SL dipasang terpisah setelah
                                  fill). LIMIT sudah punya SL dari Step 12,
                                  jadi di-skip di sini supaya tidak dobel.]
    ↓ recheck_existing_positions() [leverage engine Step 10]
    ↓ notify result

`execute_signal(evaluation, conflict_action)` dipakai oleh:
  - inline signal_confirm ("Eksekusi" button — sinyal ambigu yang dikonfirmasi manual)
  - inline conflict_confirm ("Tambah" / "Replace" button)
  - proses nominal di atas (PROCEED / ADD)
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

from bot.circuit_breaker.manager import CBOpenError, get_circuit_breaker
from bot.executor.open_position import ExecutionResult, open_position
from bot.executor.order_manager import (
    OrderManagementResult,
    cancel_pending_order,
    close_position,
    set_stop_loss,
)
from bot.leverage_engine.leverage_engine import (
    ExistingPositionSafetyAlert,
    format_existing_position_alert,
    format_leverage_safety_notification,
    recheck_existing_positions,
    run_leverage_safety_check,
)
from bot.parser.ambiguity import SignalEvaluation, evaluate_signal
from bot.parser.signal_parser import ParsedSignal
from bot.position_checker.position_checker import (
    PositionCheckResult,
    check_position_condition,
    format_position_check_notification,
)
from bot.risk_engine.risk_engine import (
    RiskCalculationResult,
    calculate_trade_risk,
    format_risk_notification,
)
from config.settings import settings
from core.constants import (
    Component,
    EntryType,
    EventType,
    ParseStatus,
    PositionAction,
    Severity,
)
from core.logging_setup import get_logger
from db.crud.event_log import async_log_event
from db.crud.settings import async_is_bot_paused
from db.crud.signal_log import (
    async_create_signal_log,
    async_is_message_processed,
    async_update_signal_action,
)
from db.crud.trades import get_open_trades
from exchange.bitget.market_data import get_default_market_cache
from exchange.bitget.rest_client import get_rest_client
from exchange.bitget.retry import CriticalError
from notifications.notifier import notify
from telegram import Bot

logger = get_logger(__name__)


class SignalPipeline:
    """
    Orkestrator pipeline sinyal end-to-end.

    Semua komponen di-inject lewat konstruktor (atau singleton default)
    supaya mudah ditest: bisa pass mock market_validator, mock rest_client, dll.
    """

    def __init__(self) -> None:
        self._cb = get_circuit_breaker()
        self._cb.set_notify_fn(notify)

    # ── Entry point: pesan dari Telethon listener ────────────────────────

    async def process_raw_event(self, raw_event: Dict[str, Any]) -> None:
        """
        Dipanggil oleh TelegramListener.on_message untuk setiap pesan baru.
        raw_event: dict dari listener._build_raw_event()
        """
        message_id: int = raw_event.get("message_id", 0)
        text: str = raw_event.get("text", "")
        chat_id: int = raw_event.get("chat_id", 0)
        sender: str = raw_event.get("sender_username", "") or raw_event.get("sender_name", "")
        received_at: str = raw_event.get("received_at", "")

        if not text.strip():
            return

        # ── Deduplication ──────────────────────────────────────────────
        if message_id and await async_is_message_processed(message_id):
            logger.debug("[pipeline] message_id=%s sudah diproses, skip.", message_id)
            return

        # ── Evaluasi sinyal ────────────────────────────────────────────
        try:
            cache = get_default_market_cache()
            evaluation = await evaluate_signal(
                text, market_validator=cache.find_symbol
            )
        except Exception as exc:
            logger.exception("[pipeline] evaluate_signal error: %s", exc)
            await async_create_signal_log(
                message_id=message_id,
                chat_id=chat_id,
                sender_username=sender,
                raw_text=text,
                received_at=received_at,
                parsed_status="error",
            )
            await notify(f"⚠️ Error parsing sinyal:\n<code>{exc}</code>")
            return

        # ── Log ke signal_log ──────────────────────────────────────────
        log_id: Optional[int] = await async_create_signal_log(
            message_id=message_id,
            chat_id=chat_id,
            sender_username=sender,
            raw_text=text,
            received_at=received_at,
            parsed_status=evaluation.parse_status,
        )

        # ── Routing berdasarkan parse_status ───────────────────────────
        if evaluation.parse_status == ParseStatus.INFO_ONLY:
            await self._handle_info_only(evaluation, log_id)
            return

        if evaluation.parse_status == ParseStatus.AMBIGUOUS:
            await self._handle_ambiguous(evaluation, log_id, message_id)
            return

        if evaluation.parse_status != ParseStatus.SUCCESS:
            logger.warning("[pipeline] parse_status tidak dikenal: %s", evaluation.parse_status)
            return

        # ── Sinyal SUCCESS — lanjut eksekusi ───────────────────────────
        await self._execute_valid_signal(evaluation, log_id, conflict_action=None)

    # ── Handle info-only ─────────────────────────────────────────────────

    async def _handle_info_only(
        self, evaluation: SignalEvaluation, log_id: Optional[int]
    ) -> None:
        info = evaluation.info
        if info is None:
            return
        msg = (
            f"ℹ️ <b>Update posisi</b> — {info.event_type.replace('_', ' ').title()}\n"
            f"Pair: <code>{info.pair_raw}</code>"
        )
        if info.r_multiple is not None:
            msg += f" | R: <b>{info.r_multiple}</b>"
        await notify(msg)
        logger.info("[pipeline] INFO_ONLY %s %s R=%s", info.event_type, info.pair_raw, info.r_multiple)

    # ── Handle ambiguous — kirim ke inline confirm ───────────────────────

    async def _handle_ambiguous(
        self,
        evaluation: SignalEvaluation,
        log_id: Optional[int],
        message_id: int,
    ) -> None:
        logger.warning("[pipeline] Sinyal AMBIGU — kirim ke konfirmasi manual.")
        try:
            from bot.control_bot.inline.signal_confirm import send_ambiguous_confirm

            bot = Bot(token=settings.TELEGRAM_BOT_TOKEN)
            await send_ambiguous_confirm(
                bot=bot,
                chat_id=settings.TELEGRAM_CONTROL_CHAT_ID,
                evaluation=evaluation,
                signal_message_id=message_id,
            )
        except Exception as exc:
            logger.error("[pipeline] Gagal kirim ambiguous confirm: %s", exc)
            await notify(
                f"⚠️ <b>SINYAL AMBIGU</b>\n\n"
                f"<blockquote>{evaluation.raw_text[:300]}</blockquote>\n\n"
                f"Alasan: {', '.join(evaluation.ambiguous_reasons[:3])}\n"
                f"(Gagal kirim inline button: {exc})"
            )

    # ── Execute valid signal ─────────────────────────────────────────────

    async def execute_signal(
        self,
        evaluation: SignalEvaluation,
        *,
        conflict_action: Optional[str] = None,
    ) -> str:
        """
        Eksekusi sinyal yang sudah di-evaluate (SUCCESS).
        Dipakai oleh:
          - proses nominal (process_raw_event)
          - inline signal_confirm "Eksekusi" button
          - inline conflict_confirm "Tambah" / "Replace" button

        Returns: teks ringkasan untuk ditampilkan di Telegram (HTML).
        """
        return await self._execute_valid_signal(
            evaluation,
            log_id=None,
            conflict_action=conflict_action,
        )

    async def _execute_valid_signal(
        self,
        evaluation: SignalEvaluation,
        log_id: Optional[int],
        conflict_action: Optional[str],
    ) -> str:
        parsed = evaluation.parsed
        if parsed is None:
            return "❌ parsed=None — sinyal tidak bisa dieksekusi."

        pair = parsed.pair_normalized or parsed.pair_raw or ""
        if not pair:
            return "❌ Pair tidak tersedia."

        # ── Cek bot paused ────────────────────────────────────────────
        if await async_is_bot_paused():
            msg = (
                f"⏸️ <b>Bot dalam mode PAUSED</b> — sinyal {pair} diabaikan.\n"
                f"Kirim <code>/resume</code> untuk mengaktifkan kembali."
            )
            await notify(msg)
            logger.info("[pipeline] Bot paused — sinyal %s diabaikan.", pair)
            return msg

        # ── Cek circuit breaker ───────────────────────────────────────
        # PENTING: parsing (evaluation.parsed) SUDAH SELESAI sebelum method
        # ini dipanggil — _run_full_pipeline di bawah ini isinya risk_engine
        # + leverage_engine + executor (order placement), BUKAN parsing.
        # Makanya di-tag Component.ORDER_EXECUTION, konsisten dengan semua
        # event_log di order_manager.py/open_position.py yang juga pakai
        # ORDER_EXECUTION untuk error di tahap ini. Sebelumnya salah di-tag
        # Component.SIGNAL_PARSER — akibatnya CriticalError dari executor
        # (mis. "insufficient funds", "amount below minimum") ikut men-trip
        # breaker SIGNAL_PARSER, yang lalu memblokir SEMUA sinyal baru
        # (termasuk yang parsing-nya sukses & valid) sampai /resume manual —
        # padahal parser-nya sendiri tidak pernah bermasalah.
        cb = self._cb
        try:
            await cb.execute_with_cb(
                Component.ORDER_EXECUTION,
                self._run_full_pipeline(parsed, pair, log_id, conflict_action),
            )
            return "✅ Pipeline selesai."
        except CBOpenError as exc:
            msg = f"🔴 {exc}"
            await notify(msg)
            return msg
        except CriticalError as exc:
            msg = f"🔴 Critical error pipeline: {exc}"
            logger.error("[pipeline] %s", msg)
            await notify(msg)
            return msg
        except Exception as exc:
            msg = f"❌ Error tidak terduga: {exc}"
            logger.exception("[pipeline] %s", exc)
            return msg

    async def _run_full_pipeline(
        self,
        parsed: ParsedSignal,
        pair: str,
        log_id: Optional[int],
        conflict_action: Optional[str],
    ) -> None:
        """Pipeline inti: position check → risk → leverage → executor → SL → recheck."""
        client = get_rest_client()

        # ── Position check (Step 11) ──────────────────────────────────
        if conflict_action is None:
            pos_check = await check_position_condition(pair, rest_client=client)
            action = await self._handle_position_check(pos_check, parsed, pair)
            if action is None:
                return   # skip atau ASK_CONFIRMATION — pipeline berhenti
            conflict_action = action if action else None  # "" → None (PROCEED)
        # Jika conflict_action sudah di-set (dari inline button): langsung lanjut

        # ── Risk engine (Step 9) ──────────────────────────────────────
        risk = await calculate_trade_risk(
            pair=pair,
            direction=parsed.direction,
            entry_type=parsed.entry_type or EntryType.MARKET,
            entry_price=parsed.entry_price,
            sl_price=parsed.stop_loss or 0.0,
            rest_client=client,
        )
        if not risk.success:
            msg = format_risk_notification(risk)
            await notify(msg)
            logger.warning("[pipeline] Risk engine gagal: %s", risk.failure_reason)
            return

        # ── Leverage safety (Step 10) ─────────────────────────────────
        safety = await run_leverage_safety_check(
            pair=pair,
            direction=parsed.direction or "long",
            entry_price=risk.entry_price_used or parsed.entry_price or 0.0,
            sl_price=risk.sl_price or 0.0,
            position_size=risk.position_size or 0.0,
            initial_leverage=risk.leverage_used or 1.0,
            max_leverage_available=risk.max_leverage_available or 1.0,
            rest_client=client,
        )
        if not safety.success:
            msg = format_leverage_safety_notification(safety)
            await notify(msg)
            logger.warning("[pipeline] Leverage safety gagal: %s", safety.failure_reason)
            return

        # Kirim notif leverage adjustment jika terjadi
        if safety.leverage_adjusted or safety.even_min_leverage_unsafe:
            await notify(format_leverage_safety_notification(safety))

        # even_min_leverage_unsafe == True berarti leverage_engine SUDAH
        # membuktikan secara matematis: bahkan di leverage 1x, proyeksi
        # liquidation tidak punya buffer aman ke SL — biasanya akibat
        # position_size yang kebesaran (jarak SL terlalu sempit relatif ke
        # entry_price, lihat risk_engine: position_size = risk_amount /
        # sl_distance, SL sempit → position_size raksasa).
        #
        # Kalau FORCE_MAX_LEVERAGE AKTIF (default, keputusan user): leverage
        # SENGAJA tidak diturunkan (tetap di max) — margin_needed jadi kecil
        # di leverage tinggi, jadi TIDAK abort di sini, lanjut ke executor.
        # max_loss tetap dijamin 1% oleh risk_engine SELAMA SL sempat fill;
        # notif peringatan di atas sudah cukup untuk kasus ini.
        #
        # Kalau FORCE_MAX_LEVERAGE MATI: leverage_engine tetap memakai
        # leverage minimum (1x) yang sudah terbukti tidak aman — posisi
        # seukuran itu hampir pasti juga tidak lolos margin check exchange
        # (insufficient funds). Daripada mencoba open_position dan gagal
        # dengan pesan exchange yang membingungkan, pipeline berhenti DI SINI
        # dengan alasan yang jelas.
        if safety.even_min_leverage_unsafe and not settings.FORCE_MAX_LEVERAGE:
            sl_note = (
                f"Kemungkinan besar penyebabnya: jarak Stop Loss ke Entry TERLALU "
                f"SEMPIT ({risk.sl_distance:g} dari entry {risk.entry_price_used:g} "
                f"→ position_size jadi sangat besar untuk tetap risk 1%).\n\n"
                f"<i>Sinyal tidak dieksekusi — cek ulang harga SL, atau perkecil "
                f"risk per trade kalau SL memang sesempit ini.</i>"
                if risk.sl_distance is not None and risk.entry_price_used is not None
                else "<i>Sinyal tidak dieksekusi.</i>"
            )
            await notify(
                f"🛑 <b>SINYAL {pair} DIBATALKAN</b>\n\n"
                f"Leverage safety check sudah membuktikan posisi ini TIDAK bisa "
                f"aman bahkan di leverage 1x (lihat detail di atas). {sl_note}"
            )
            logger.warning(
                "[pipeline] %s dibatalkan — even_min_leverage_unsafe=True, "
                "FORCE_MAX_LEVERAGE=False, position_size=%s tidak akan lolos "
                "margin check di exchange.",
                pair, risk.position_size,
            )
            return

        # ── Open position (Step 12) ───────────────────────────────────
        exec_result: ExecutionResult = await open_position(
            signal=parsed,
            risk=risk,
            safety=safety,
            conflict_action=conflict_action,
            rest_client=client,
            dry_run=settings.DRY_RUN,
        )

        await notify(exec_result.notification_text())

        if not exec_result.success:
            if exec_result.is_critical:
                raise CriticalError(
                    f"[executor] open_position critical: {exec_result.failure_reason}"
                )
            return

        # ── Set SL (Step 13) — HANYA untuk market order ─────────────────
        # Limit order SUDAH wajib bawa SL attached (params.stopLossPrice)
        # sejak open_position() (Step 12) — kalau sl_price kosong, order
        # ditolak DI SANA, tidak pernah sampai sini. Panggil set_stop_loss
        # lagi di sini untuk limit order akan bikin SL DOBEL di exchange.
        if parsed.entry_type != EntryType.LIMIT and not settings.DRY_RUN:
            if exec_result.trade_id is None:
                await notify(
                    f"⚠️ Order {pair} terkirim tapi trade_id tidak tercatat di DB "
                    f"(lihat notif error sebelumnya) — SL TIDAK bisa diset otomatis.\n"
                    f"Set SL manual segera di exchange!"
                )
            else:
                try:
                    sl_result: OrderManagementResult = await set_stop_loss(
                        trade_id=exec_result.trade_id,
                        sl_price=parsed.stop_loss or risk.sl_price or 0.0,
                        rest_client=client,
                    )
                except Exception as exc:
                    # Defense in depth: apapun yang salah di sini (bug baru, error
                    # tak terduga, dll) TIDAK BOLEH cuma ke-log diam-diam — posisi
                    # sudah live di exchange TANPA proteksi SL, user wajib tahu
                    # SEKARANG, bukan nanti pas cek log.
                    logger.exception(
                        "[pipeline] set_stop_loss meledak tak terduga untuk %s (trade_id=%s)",
                        pair, exec_result.trade_id,
                    )
                    await notify(
                        f"🔴 <b>SL GAGAL DISET</b> untuk {pair} (error tak terduga: {exc})\n"
                        f"Trade #{exec_result.trade_id} — <b>SET SL MANUAL SEKARANG</b> di exchange!"
                    )
                else:
                    if not sl_result.success:
                        await notify(
                            f"⚠️ Gagal set SL untuk {pair}: {sl_result.failure_reason}\n"
                            f"Set SL manual segera!"
                        )
                    else:
                        logger.info("[pipeline] SL set sukses untuk %s", pair)
        elif settings.DRY_RUN:
            logger.info("[pipeline][DRY-RUN] SL tidak dikirim ke exchange.")

        # Update signal_log action
        if log_id:
            await async_update_signal_action(
                log_id, action_taken="executed", trade_id=exec_result.trade_id
            )

        # ── Recheck posisi existing (Step 10, bagian 4.3 langkah 5) ──
        await self._recheck_and_alert(client, pair)

    async def _handle_position_check(
        self,
        pos_check: PositionCheckResult,
        parsed: ParsedSignal,
        pair: str,
    ) -> Optional[str]:
        """
        Tangani hasil position_check.
        Returns: conflict_action string untuk dipakai executor,
                 atau None jika pipeline harus berhenti (skip / ask).
        """
        if not pos_check.success:
            msg = (
                f"⚠️ Gagal cek posisi untuk {pair}: {pos_check.failure_reason}\n"
                f"Sinyal diabaikan untuk keamanan."
            )
            await notify(msg)
            return None

        action = pos_check.recommended_action

        if action == PositionAction.PROCEED:
            return ""  # "" = lanjut pipeline tanpa conflict_action

        if action == PositionAction.SKIP:
            await notify(
                f"⏭️ Sinyal {pair} diabaikan — ada posisi/pending, "
                f"conflict mode = skip."
            )
            return None  # stop

        if action == PositionAction.ADD:
            await notify(
                f"➕ Membuka posisi tambahan untuk {pair} "
                f"(conflict mode = add)."
            )
            return PositionAction.ADD

        if action == PositionAction.REPLACE:
            await self._do_replace(pair, pos_check)
            return PositionAction.REPLACE

        if action == PositionAction.ASK_CONFIRMATION:
            await self._send_conflict_confirm(pos_check, parsed, pair)
            return None  # pipeline berhenti, tunggu inline button

        return None

    async def _do_replace(self, pair: str, pos_check: PositionCheckResult) -> None:
        """Cancel pending order atau close posisi lama sebelum Replace."""
        client = get_rest_client()
        existing = pos_check.db_trade or {}
        trade_id = existing.get("id")
        condition = pos_check.condition

        from core.constants import PositionCondition
        if condition in (
            PositionCondition.PENDING_ORDER, PositionCondition.OPEN_AND_PENDING
        ):
            if trade_id:
                result = await cancel_pending_order(
                    trade_id=trade_id, pair=pair, rest_client=client
                )
                if not result.success:
                    await notify(f"⚠️ Gagal cancel pending order {pair}: {result.failure_reason}")

        if condition in (
            PositionCondition.OPEN_POSITION, PositionCondition.OPEN_AND_PENDING
        ):
            if trade_id:
                result = await close_position(
                    trade_id=trade_id, pair=pair,
                    direction=existing.get("direction", "long"),
                    rest_client=client,
                )
                if not result.success:
                    await notify(f"⚠️ Gagal close posisi lama {pair}: {result.failure_reason}")

    async def _send_conflict_confirm(
        self,
        pos_check: PositionCheckResult,
        parsed: ParsedSignal,
        pair: str,
    ) -> None:
        """Kirim inline button konfirmasi konflik ke control chat."""
        try:
            from bot.control_bot.inline.conflict_confirm import send_conflict_confirm
            from core.constants import PositionCondition

            bot = Bot(token=settings.TELEGRAM_BOT_TOKEN)
            existing_trade = pos_check.db_trade or {}
            if not existing_trade and pos_check.live_position:
                lp = pos_check.live_position
                existing_trade = {
                    "direction": lp.direction,
                    "entry_price": lp.entry_price,
                    "sl_price": lp.sl_price,
                    "id": None,
                }

            conflict_type = (
                "pending"
                if pos_check.condition == PositionCondition.PENDING_ORDER
                else "open"
            )
            new_signal_data = {
                "direction": parsed.direction,
                "entry_type": parsed.entry_type,
                "entry_price": parsed.entry_price,
                "sl_price": parsed.stop_loss,
                "_evaluation": None,  # filled by caller for full execution
            }

            await send_conflict_confirm(
                bot=bot,
                chat_id=settings.TELEGRAM_CONTROL_CHAT_ID,
                pair=pair,
                existing_trade=existing_trade,
                new_signal_data=new_signal_data,
                conflict_type=conflict_type,
            )
        except Exception as exc:
            logger.error("[pipeline] Gagal kirim conflict confirm: %s", exc)
            await notify(
                f"⚠️ Konflik posisi untuk {pair} — tidak bisa kirim inline button: {exc}\n"
                f"Tangani manual."
            )

    async def _recheck_and_alert(self, client, pair: str) -> None:
        """Re-check posisi existing setelah entry baru, alert jika tidak aman."""
        # Ambil SL dari database untuk semua posisi
        sl_lookup: Dict[str, float] = {}
        try:
            open_trades = get_open_trades()
            for t in open_trades:
                sym = t.get("pair", "")
                sl = t.get("sl_price")
                if sym and sl:
                    sl_lookup[sym] = float(sl)
        except Exception:
            pass

        alerts: list[ExistingPositionSafetyAlert] = await recheck_existing_positions(
            rest_client=client, sl_lookup=sl_lookup
        )
        for alert in alerts:
            await notify(format_existing_position_alert(alert))
            await async_log_event(
                EventType.LIQUIDATION_WARNING,
                f"Posisi {alert.symbol} tidak aman setelah entry baru ({pair})",
                component=Component.ORDER_EXECUTION,
                severity=Severity.WARNING,
            )


# ── Singleton ────────────────────────────────────────────────────────────

_pipeline: Optional[SignalPipeline] = None


def get_pipeline() -> SignalPipeline:
    global _pipeline
    if _pipeline is None:
        _pipeline = SignalPipeline()
    return _pipeline