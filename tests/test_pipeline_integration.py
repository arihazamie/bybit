"""
tests/test_pipeline_integration.py
=====================================
Step 19 — End-to-end dry-run tests untuk SignalPipeline.

Skenario yang diuji:
  1. Sinyal valid → pipeline eksekusi (dry-run)
  2. Sinyal ambigu → kirim ambiguous_confirm, tidak eksekusi
  3. Info-only → log + notify, tidak eksekusi
  4. Konflik posisi (open_position) + conflict_mode=ask → inline confirm
  5. Insufficient margin → notify, tidak eksekusi
  6. Error transient dari executor → pipeline graceful
  7. Error critical dari executor → circuit breaker trip
  8. Circuit breaker OPEN → pipeline tolak eksekusi baru
  9. Bot paused → pipeline tolak eksekusi

Semua test pakai mock untuk exchange, database, dan Telegram.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ── Fixtures & helpers ───────────────────────────────────────────────────


VALID_SIGNAL = """🚀 SWING SETUP - LONG/buy

🔘 Pair : $STG

🔘 Time frame : 4H

🔘 Entry limit 0.45

🔘 Target : di chart

🔘 Stop loss : 0.40

🔖 ENTRY REASON : Bullish structure

🔫 Risk Adjustment :
*Max Loss / Risk Per Trade 1% of Total Trading Balance*
"""

AMBIGUOUS_SIGNAL = "maybe long maybe short $XYZ entry unknown"

INFO_SIGNAL_HIT = "$STG hit entry"
INFO_SIGNAL_CLOSE = "$STG Close 2.5R"


def _make_mock_evaluation(parse_status: str, pair: str = "STG/USDT:USDT"):
    """Bangun SignalEvaluation mock dengan ParsedSignal untuk tests."""
    from bot.parser.signal_parser import ParsedSignal
    from bot.parser.ambiguity import SignalEvaluation
    from core.constants import ParseStatus, MessageType, EntryType, Direction

    parsed = ParsedSignal(
        raw_text=VALID_SIGNAL,
        direction=Direction.LONG,
        pair_raw="STG",
        pair_normalized=pair,
        entry_type=EntryType.LIMIT,
        entry_price=0.45,
        stop_loss=0.40,
        symbol_valid=True,
        parse_status=parse_status,
    )
    return SignalEvaluation(
        raw_text=VALID_SIGNAL,
        message_type=MessageType.NEW_SIGNAL_CANDIDATE,
        parse_status=parse_status,
        parsed=parsed,
        confidence=97,
    )


def _make_raw_event(text: str, message_id: int = 100) -> dict:
    return {
        "message_id": message_id,
        "chat_id": 123,
        "sender_username": "analyst",
        "sender_name": "Test Analyst",
        "text": text,
        "received_at": "2024-01-01T00:00:00+00:00",
    }


# ── Test 1: Sinyal valid → dry-run eksekusi ──────────────────────────────

@pytest.mark.asyncio
async def test_valid_signal_dry_run():
    """Sinyal valid → pipeline memanggil open_position dengan dry_run=True."""
    from core.constants import ParseStatus
    evaluation = _make_mock_evaluation(ParseStatus.SUCCESS)

    from bot.position_checker.position_checker import PositionCheckResult
    from bot.risk_engine.risk_engine import RiskCalculationResult
    from bot.leverage_engine.leverage_engine import LeverageSafetyResult
    from bot.executor.open_position import ExecutionResult
    from core.constants import PositionAction

    exec_result = ExecutionResult(
        success=True, pair="STG/USDT:USDT",
        trade_id=1, is_dry_run=True,
    )
    exec_result.notification_text = lambda: "✅ [DRY-RUN] Order placed"

    async def _run_coro(component, coro, **kwargs):
        return await coro

    with (
        patch("bot.pipeline.signal_pipeline.async_is_bot_paused", return_value=False),
        patch("bot.pipeline.signal_pipeline.check_position_condition",
              return_value=PositionCheckResult(
                  success=True, pair="STG/USDT:USDT",
                  condition="none", recommended_action=PositionAction.PROCEED,
              )),
        patch("bot.pipeline.signal_pipeline.calculate_trade_risk",
              return_value=RiskCalculationResult(
                  success=True, sl_price=0.40,
                  risk_amount_usd=5.0, position_size=100.0,
                  margin_needed=2.25, leverage_used=20.0,
                  max_leverage_available=20.0, risk_mode="percent",
                  entry_price_used=0.45,
              )),
        patch("bot.pipeline.signal_pipeline.run_leverage_safety_check",
              return_value=LeverageSafetyResult(
                  success=True, leverage_requested=20.0,
                  leverage_safe=20.0, leverage_adjusted=False,
              )),
        patch("bot.pipeline.signal_pipeline.open_position", return_value=exec_result) as mock_exec,
        patch("bot.pipeline.signal_pipeline.recheck_existing_positions", return_value=[]),
        patch("bot.pipeline.signal_pipeline.notify", new_callable=AsyncMock),
        patch("bot.pipeline.signal_pipeline.get_rest_client"),
        patch("bot.pipeline.signal_pipeline.async_update_signal_action", new_callable=AsyncMock),
        patch("bot.pipeline.signal_pipeline.get_open_trades", return_value=[]),
    ):
        from bot.pipeline.signal_pipeline import SignalPipeline
        pipeline = SignalPipeline()
        pipeline._cb.execute_with_cb = _run_coro
        result = await pipeline.execute_signal(evaluation, conflict_action=None)

    mock_exec.assert_called_once()
    assert "✅" in result or "Pipeline" in result or "selesai" in result


# ── Test 1b: Sinyal MARKET (non dry-run) → set_stop_loss dipanggil dengan
#             signature yang BENAR (regression test untuk bug TypeError
#             'pair'/'direction' unexpected keyword argument) ────────────

@pytest.mark.asyncio
async def test_market_signal_calls_set_stop_loss_with_correct_signature():
    """
    Regression test: pipeline sempat memanggil set_stop_loss(trade_id=.., pair=..,
    direction=.., sl_price=.., rest_client=..) padahal signature aslinya di
    order_manager.py cuma (trade_id, sl_price, *, rest_client, dry_run) — extra
    kwargs 'pair'/'direction' bikin TypeError yang ke-swallow diam-diam oleh
    except Exception generik di _execute_valid_signal, jadi SL TIDAK PERNAH
    benar-benar terkirim ke exchange walau notifikasi entry sudah "✅ sukses".

    Test ini pakai entry_type=MARKET + DRY_RUN=False (satu-satunya kondisi yang
    memicu pemanggilan set_stop_loss — lihat test_valid_signal_dry_run yang
    selalu DRY_RUN=True dan skip jalur ini sepenuhnya) dan assert set_stop_loss
    dipanggil TANPA kwargs 'pair'/'direction'.
    """
    from core.constants import ParseStatus, EntryType, Direction

    evaluation = _make_mock_evaluation(ParseStatus.SUCCESS)
    evaluation.parsed.entry_type = EntryType.MARKET
    evaluation.parsed.entry_price = None

    from bot.position_checker.position_checker import PositionCheckResult
    from bot.risk_engine.risk_engine import RiskCalculationResult
    from bot.leverage_engine.leverage_engine import LeverageSafetyResult
    from bot.executor.open_position import ExecutionResult
    from bot.executor.order_manager import OrderManagementResult
    from core.constants import PositionAction

    exec_result = ExecutionResult(
        success=True, pair="STG/USDT:USDT",
        trade_id=42, is_dry_run=False,
    )
    exec_result.notification_text = lambda: "✅ Order placed"

    sl_result = OrderManagementResult(
        success=True, operation="set_sl", pair="STG/USDT:USDT",
        trade_id=42, sl_price=0.40,
    )

    from config.settings import settings as real_settings
    import dataclasses
    patched_settings = dataclasses.replace(real_settings, DRY_RUN=False)

    async def _run_coro(component, coro, **kwargs):
        return await coro

    with (
        patch("bot.pipeline.signal_pipeline.async_is_bot_paused", return_value=False),
        patch("bot.pipeline.signal_pipeline.check_position_condition",
              return_value=PositionCheckResult(
                  success=True, pair="STG/USDT:USDT",
                  condition="none", recommended_action=PositionAction.PROCEED,
              )),
        patch("bot.pipeline.signal_pipeline.calculate_trade_risk",
              return_value=RiskCalculationResult(
                  success=True, sl_price=0.40,
                  risk_amount_usd=5.0, position_size=100.0,
                  margin_needed=2.25, leverage_used=20.0,
                  max_leverage_available=20.0, risk_mode="percent",
                  entry_price_used=0.45,
              )),
        patch("bot.pipeline.signal_pipeline.run_leverage_safety_check",
              return_value=LeverageSafetyResult(
                  success=True, leverage_requested=20.0,
                  leverage_safe=20.0, leverage_adjusted=False,
              )),
        patch("bot.pipeline.signal_pipeline.open_position", return_value=exec_result),
        patch("bot.pipeline.signal_pipeline.set_stop_loss",
              new_callable=AsyncMock, return_value=sl_result) as mock_sl,
        patch("bot.pipeline.signal_pipeline.recheck_existing_positions", return_value=[]),
        patch("bot.pipeline.signal_pipeline.notify", new_callable=AsyncMock),
        patch("bot.pipeline.signal_pipeline.get_rest_client"),
        patch("bot.pipeline.signal_pipeline.async_update_signal_action", new_callable=AsyncMock),
        patch("bot.pipeline.signal_pipeline.get_open_trades", return_value=[]),
        patch("bot.pipeline.signal_pipeline.settings", patched_settings),
    ):
        from bot.pipeline.signal_pipeline import SignalPipeline
        pipeline = SignalPipeline()
        pipeline._cb.execute_with_cb = _run_coro
        result = await pipeline.execute_signal(evaluation, conflict_action=None)

    # Kalau bug signature-nya balik lagi, mock_sl akan gagal di-assert_called_once
    # (TypeError terjadi SEBELUM/SAAT call, kepatch mock jadi tidak pernah
    # ke-invoke dengan kwargs yang salah — assert kwargs eksplisit ini yang
    # jadi jaring pengaman utama).
    mock_sl.assert_called_once()
    _, kwargs = mock_sl.call_args
    assert "pair" not in kwargs, "set_stop_loss dipanggil dengan kwarg 'pair' yang tidak ada di signature aslinya"
    assert "direction" not in kwargs, "set_stop_loss dipanggil dengan kwarg 'direction' yang tidak ada di signature aslinya"
    assert kwargs.get("trade_id") == 42
    assert kwargs.get("sl_price") == 0.40
    assert "✅" in result or "Pipeline" in result or "selesai" in result


# ── Test 2: Sinyal ambigu → tidak eksekusi ───────────────────────────────

@pytest.mark.asyncio
async def test_ambiguous_signal_not_executed():
    """Sinyal ambigu → kirim ambiguous_confirm, open_position tidak dipanggil."""
    raw_event = _make_raw_event(AMBIGUOUS_SIGNAL, message_id=101)

    with (
        patch("bot.pipeline.signal_pipeline.async_is_message_processed", return_value=False),
        patch("bot.pipeline.signal_pipeline.async_create_signal_log", return_value=1),
        patch("bot.pipeline.signal_pipeline.evaluate_signal") as mock_eval,
        patch("bot.pipeline.signal_pipeline.open_position") as mock_exec,
        patch("bot.pipeline.signal_pipeline.notify", new_callable=AsyncMock),
        patch("bot.pipeline.signal_pipeline.get_default_market_cache"),
        patch("bot.pipeline.signal_pipeline.Bot") as mock_bot_cls,
    ):
        from bot.parser.ambiguity import SignalEvaluation
        from core.constants import ParseStatus, MessageType
        mock_eval.return_value = SignalEvaluation(
            raw_text=AMBIGUOUS_SIGNAL,
            message_type=MessageType.NEW_SIGNAL_CANDIDATE,
            parse_status=ParseStatus.AMBIGUOUS,
            ambiguous_reasons=["Pair tidak dikenali"],
            confidence=40,
        )

        mock_bot_instance = AsyncMock()
        mock_bot_cls.return_value = mock_bot_instance

        with patch(
            "bot.control_bot.inline.signal_confirm.send_ambiguous_confirm",
            new_callable=AsyncMock,
        ):
            from bot.pipeline.signal_pipeline import SignalPipeline
            pipeline = SignalPipeline()
            await pipeline.process_raw_event(raw_event)

    mock_exec.assert_not_called()


# ── Test 3: Info-only → notify, tidak eksekusi ───────────────────────────

@pytest.mark.asyncio
async def test_info_only_no_execution():
    raw_event = _make_raw_event(INFO_SIGNAL_CLOSE, message_id=102)

    with (
        patch("bot.pipeline.signal_pipeline.async_is_message_processed", return_value=False),
        patch("bot.pipeline.signal_pipeline.async_create_signal_log", return_value=2),
        patch("bot.pipeline.signal_pipeline.get_default_market_cache"),
        patch("bot.pipeline.signal_pipeline.open_position") as mock_exec,
        patch("bot.pipeline.signal_pipeline.notify", new_callable=AsyncMock) as mock_notify,
    ):
        from bot.pipeline.signal_pipeline import SignalPipeline
        pipeline = SignalPipeline()
        await pipeline.process_raw_event(raw_event)

    mock_exec.assert_not_called()
    # notify harus dipanggil dengan info update
    assert mock_notify.call_count >= 1
    call_text = mock_notify.call_args_list[0][0][0]
    assert "Update posisi" in call_text or "close" in call_text.lower()


# ── Test 4: Konflik posisi + mode ask → inline button ────────────────────

@pytest.mark.asyncio
async def test_conflict_ask_sends_inline_button():
    """conflict_mode=ask → pipeline kirim conflict_confirm, tidak eksekusi."""
    from core.constants import ParseStatus
    evaluation = _make_mock_evaluation(ParseStatus.SUCCESS)

    async def _run_coro(component, coro, **kwargs):
        return await coro

    with (
        patch("bot.pipeline.signal_pipeline.async_is_bot_paused", return_value=False),
        patch("bot.pipeline.signal_pipeline.check_position_condition") as mock_pos,
        patch("bot.pipeline.signal_pipeline.open_position") as mock_exec,
        patch("bot.pipeline.signal_pipeline.notify", new_callable=AsyncMock),
        patch("bot.pipeline.signal_pipeline.get_rest_client"),
        patch("bot.pipeline.signal_pipeline.Bot") as mock_bot_cls,
    ):
        from bot.position_checker.position_checker import PositionCheckResult
        from core.constants import PositionAction, PositionCondition

        mock_pos.return_value = PositionCheckResult(
            success=True, pair="STG/USDT:USDT",
            condition=PositionCondition.OPEN_POSITION,
            recommended_action=PositionAction.ASK_CONFIRMATION,
            db_trade={"id": 5, "direction": "long", "entry_price": 0.50, "sl_price": 0.42},
        )
        mock_bot_cls.return_value = AsyncMock()

        with patch(
            "bot.control_bot.inline.conflict_confirm.send_conflict_confirm",
            new_callable=AsyncMock,
        ) as mock_conf:
            from bot.pipeline.signal_pipeline import SignalPipeline
            pipeline = SignalPipeline()
            pipeline._cb.execute_with_cb = _run_coro
            await pipeline._execute_valid_signal(evaluation, log_id=None, conflict_action=None)

    mock_exec.assert_not_called()


# ── Test 5: Insufficient margin → notify, tidak eksekusi ─────────────────

@pytest.mark.asyncio
async def test_insufficient_margin_no_execution():
    from core.constants import ParseStatus
    evaluation = _make_mock_evaluation(ParseStatus.SUCCESS)

    async def _run_coro(component, coro, **kwargs):
        return await coro

    with (
        patch("bot.pipeline.signal_pipeline.async_is_bot_paused", return_value=False),
        patch("bot.pipeline.signal_pipeline.check_position_condition") as mock_pos,
        patch("bot.pipeline.signal_pipeline.calculate_trade_risk") as mock_risk,
        patch("bot.pipeline.signal_pipeline.open_position") as mock_exec,
        patch("bot.pipeline.signal_pipeline.notify", new_callable=AsyncMock) as mock_notify,
        patch("bot.pipeline.signal_pipeline.get_rest_client"),
    ):
        from bot.position_checker.position_checker import PositionCheckResult
        from bot.risk_engine.risk_engine import RiskCalculationResult
        from core.constants import PositionAction

        mock_pos.return_value = PositionCheckResult(
            success=True, pair="STG/USDT:USDT",
            condition="none", recommended_action=PositionAction.PROCEED,
        )
        mock_risk.return_value = RiskCalculationResult(
            success=False, sl_price=0.40,
            failure_reason="insufficient_margin",
        )

        from bot.pipeline.signal_pipeline import SignalPipeline
        pipeline = SignalPipeline()
        pipeline._cb.execute_with_cb = _run_coro
        await pipeline._execute_valid_signal(evaluation, log_id=None, conflict_action=None)

    mock_exec.assert_not_called()
    assert mock_notify.call_count >= 1


# ── Test 6: Bot paused → eksekusi ditolak ────────────────────────────────

@pytest.mark.asyncio
async def test_bot_paused_blocks_execution():
    from core.constants import ParseStatus
    evaluation = _make_mock_evaluation(ParseStatus.SUCCESS)

    with (
        patch("bot.pipeline.signal_pipeline.async_is_bot_paused", return_value=True),
        patch("bot.pipeline.signal_pipeline.open_position") as mock_exec,
        patch("bot.pipeline.signal_pipeline.notify", new_callable=AsyncMock) as mock_notify,
    ):
        from bot.pipeline.signal_pipeline import SignalPipeline
        pipeline = SignalPipeline()
        result = await pipeline.execute_signal(evaluation)

    mock_exec.assert_not_called()
    assert "PAUSED" in result or "paused" in result.lower()
    assert mock_notify.call_count >= 1


# ── Test 7: Circuit breaker OPEN → eksekusi ditolak ──────────────────────

@pytest.mark.asyncio
async def test_circuit_breaker_open_blocks_execution():
    from core.constants import ParseStatus
    evaluation = _make_mock_evaluation(ParseStatus.SUCCESS)

    from bot.circuit_breaker.manager import CBOpenError

    async def _raise_cb(component, coro, **kwargs):
        coro.close()  # close coroutine agar tidak leak warning
        raise CBOpenError("CB OPEN untuk signal_parser")

    with (
        patch("bot.pipeline.signal_pipeline.async_is_bot_paused", return_value=False),
        patch("bot.pipeline.signal_pipeline.open_position") as mock_exec,
        patch("bot.pipeline.signal_pipeline.notify", new_callable=AsyncMock),
    ):
        from bot.pipeline.signal_pipeline import SignalPipeline
        pipeline = SignalPipeline()
        pipeline._cb.execute_with_cb = _raise_cb
        result = await pipeline.execute_signal(evaluation)

    mock_exec.assert_not_called()
    assert "OPEN" in result or "CB" in result or "🔴" in result


# ── Test 8: Deduplication — pesan sudah diproses ─────────────────────────

@pytest.mark.asyncio
async def test_deduplication_skips_processed_message():
    raw_event = _make_raw_event(VALID_SIGNAL, message_id=999)

    with (
        patch("bot.pipeline.signal_pipeline.async_is_message_processed", return_value=True),
        patch("bot.pipeline.signal_pipeline.evaluate_signal") as mock_eval,
    ):
        from bot.pipeline.signal_pipeline import SignalPipeline
        pipeline = SignalPipeline()
        await pipeline.process_raw_event(raw_event)

    mock_eval.assert_not_called()


# ── Test 9: Wiring — execute_fn terdaftar ────────────────────────────────

def test_wire_inline_handlers_registers_fns():
    """wire_inline_handlers() mendaftarkan _execute_fn dan _*_fn ke modul inline."""
    import bot.control_bot.inline.signal_confirm as sc
    import bot.control_bot.inline.conflict_confirm as cc

    # Reset dulu
    sc._execute_fn = None
    cc._add_fn = cc._replace_fn = cc._cancel_fn = None

    from bot.pipeline.signal_pipeline import SignalPipeline
    from bot.pipeline.wiring import wire_inline_handlers
    pipeline = SignalPipeline()
    wire_inline_handlers(pipeline)

    assert sc._execute_fn is not None
    assert cc._add_fn is not None
    assert cc._replace_fn is not None
    assert cc._cancel_fn is not None


def get_pipeline():
    from bot.pipeline.signal_pipeline import get_pipeline as _get
    return _get()