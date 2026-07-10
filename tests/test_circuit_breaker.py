"""
tests/test_circuit_breaker.py
==============================
Unit tests Step 14 — bot/circuit_breaker/manager.py

Skenario yang diuji:
  1. CLOSED → normal, on_error tidak trip sebelum threshold
  2. Threshold tepat N=3 error dalam window → trip ke OPEN
  3. Error di luar window tidak dihitung (sliding window)
  4. OPEN → execute_with_cb raise CBOpenError
  5. OPEN + is_position_monitor=True → TIDAK diblokir
  6. /resume → OPEN → HALF_OPEN
  7. Probe sukses: HALF_OPEN → CLOSED
  8. Probe gagal: HALF_OPEN → OPEN kembali
  9. Alert Telegram dikirim saat trip
  10. Alert Telegram dikirim saat reset
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.circuit_breaker.manager import CBOpenError, CircuitBreakerManager
from core.constants import CBState, Component
from exchange.bitget.retry import CriticalError


# ── Fixture ────────────────────────────────────────────────────────────────────

@pytest.fixture
def cb():
    """CircuitBreakerManager dengan threshold kecil untuk testing."""
    mgr = CircuitBreakerManager(error_threshold=3, window_minutes=5)
    notify = AsyncMock()
    mgr.set_notify_fn(notify)
    return mgr


# ── Helpers ────────────────────────────────────────────────────────────────────

def _patch_cb_state(state_str: str):
    """Patch async_get_cb_state agar return state tertentu."""
    return patch(
        "bot.circuit_breaker.manager.async_get_cb_state",
        new_callable=AsyncMock,
        return_value={"state": state_str},
    )


def _patch_is_open(value: bool):
    return patch(
        "bot.circuit_breaker.manager.async_is_cb_open",
        new_callable=AsyncMock,
        return_value=value,
    )


# ── 1. CLOSED: error di bawah threshold tidak trip ─────────────────────────────

@pytest.mark.asyncio
async def test_no_trip_below_threshold(cb):
    with (
        _patch_cb_state(CBState.CLOSED),
        patch("bot.circuit_breaker.manager.async_record_error", new_callable=AsyncMock),
        patch("bot.circuit_breaker.manager.async_trip_circuit_breaker", new_callable=AsyncMock) as mock_trip,
    ):
        await cb.on_error(Component.ORDER_EXECUTION, "err1")
        await cb.on_error(Component.ORDER_EXECUTION, "err2")
        mock_trip.assert_not_called()


# ── 2. Threshold tercapai → trip ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_trip_on_threshold(cb):
    with (
        _patch_cb_state(CBState.CLOSED),
        patch("bot.circuit_breaker.manager.async_record_error", new_callable=AsyncMock),
        patch("bot.circuit_breaker.manager.async_trip_circuit_breaker", new_callable=AsyncMock) as mock_trip,
        patch("bot.circuit_breaker.manager.async_log_event", new_callable=AsyncMock),
    ):
        for i in range(3):
            await cb.on_error(Component.ORDER_EXECUTION, f"err{i}")

        mock_trip.assert_called_once()
        # Alert Telegram harus dikirim
        cb._notify_fn.assert_called_once()
        alert_text = cb._notify_fn.call_args[0][0]
        assert "CIRCUIT BREAKER TRIP" in alert_text
        assert Component.ORDER_EXECUTION in alert_text


# ── 3. Sliding window: error lama tidak dihitung ────────────────────────────────

@pytest.mark.asyncio
async def test_sliding_window_prune(cb):
    """Error yang masuk lebih dari T menit lalu tidak dihitung."""
    old_time = datetime.now(timezone.utc) - timedelta(minutes=10)
    cb._error_times[Component.SIGNAL_PARSER].append(old_time)
    cb._error_times[Component.SIGNAL_PARSER].append(old_time)

    with (
        _patch_cb_state(CBState.CLOSED),
        patch("bot.circuit_breaker.manager.async_record_error", new_callable=AsyncMock),
        patch("bot.circuit_breaker.manager.async_trip_circuit_breaker", new_callable=AsyncMock) as mock_trip,
    ):
        # Hanya 1 error baru (total dalam window = 1, bukan 3)
        await cb.on_error(Component.SIGNAL_PARSER, "fresh_error")
        mock_trip.assert_not_called()


# ── 4. OPEN → execute_with_cb raise CBOpenError ────────────────────────────────

@pytest.mark.asyncio
async def test_open_blocks_execution(cb):
    executed = []

    async def guarded_coro():
        executed.append(True)
        return "ok"

    with _patch_cb_state(CBState.OPEN):
        with pytest.raises(CBOpenError):
            await cb.execute_with_cb(Component.ORDER_EXECUTION, guarded_coro())

    assert not executed, "Coro body should NOT have run when CB is OPEN"


# ── 5. OPEN + is_position_monitor=True → TIDAK diblokir ────────────────────────

@pytest.mark.asyncio
async def test_position_monitor_exempt_from_open(cb):
    dummy_coro = AsyncMock(return_value="positions_data")

    with (
        _patch_cb_state(CBState.CLOSED),  # on_success check
        patch("bot.circuit_breaker.manager.async_reset_error_count", new_callable=AsyncMock),
    ):
        result = await cb.execute_with_cb(
            Component.BITGET_CONNECTION,
            dummy_coro(),
            is_position_monitor=True,
        )

    assert result == "positions_data"
    dummy_coro.assert_called_once()


@pytest.mark.asyncio
async def test_position_monitor_not_blocked_even_when_open(cb):
    """is_position_monitor=True bypass semua CB state check termasuk OPEN."""
    call_count = 0

    async def monitor_coro():
        nonlocal call_count
        call_count += 1
        return "ok"

    with (
        patch("bot.circuit_breaker.manager.async_get_cb_state",
              new_callable=AsyncMock,
              return_value={"state": CBState.OPEN}),
        patch("bot.circuit_breaker.manager.async_reset_error_count", new_callable=AsyncMock),
    ):
        # Patch on_success to do nothing
        cb.on_success = AsyncMock()
        result = await cb.execute_with_cb(
            Component.BITGET_CONNECTION,
            monitor_coro(),
            is_position_monitor=True,
        )

    assert call_count == 1
    assert result == "ok"


# ── 6. /resume: OPEN → HALF_OPEN ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_resume_transitions_to_half_open(cb):
    with (
        patch("bot.circuit_breaker.manager.async_transition_to_half_open",
              new_callable=AsyncMock, return_value=True) as mock_trans,
        patch("bot.circuit_breaker.manager.async_log_event", new_callable=AsyncMock),
    ):
        result = await cb.resume(Component.ORDER_EXECUTION)

    assert Component.ORDER_EXECUTION in result
    mock_trans.assert_called_once_with(Component.ORDER_EXECUTION)


@pytest.mark.asyncio
async def test_resume_all_components(cb):
    with (
        patch("bot.circuit_breaker.manager.async_resume_all_components",
              new_callable=AsyncMock,
              return_value=[Component.ORDER_EXECUTION, Component.BITGET_CONNECTION]) as mock_all,
        patch("bot.circuit_breaker.manager.async_log_event", new_callable=AsyncMock),
    ):
        result = await cb.resume()

    assert len(result) == 2
    mock_all.assert_called_once()


# ── 7. Probe sukses: HALF_OPEN → CLOSED ────────────────────────────────────────

@pytest.mark.asyncio
async def test_halfopen_probe_success_resets_to_closed(cb):
    dummy_coro = AsyncMock(return_value="filled")

    with (
        patch("bot.circuit_breaker.manager.async_get_cb_state",
              new_callable=AsyncMock,
              return_value={"state": CBState.HALF_OPEN}),
        patch("bot.circuit_breaker.manager.async_reset_circuit_breaker",
              new_callable=AsyncMock) as mock_reset,
        patch("bot.circuit_breaker.manager.async_log_event", new_callable=AsyncMock),
    ):
        result = await cb.execute_with_cb(Component.ORDER_EXECUTION, dummy_coro())

    assert result == "filled"
    mock_reset.assert_called_once_with(Component.ORDER_EXECUTION)
    # Notifikasi reset harus dikirim
    cb._notify_fn.assert_called_once()
    assert "RESET" in cb._notify_fn.call_args[0][0]


# ── 8. Probe gagal: HALF_OPEN → OPEN ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_halfopen_probe_failure_returns_to_open(cb):
    async def failing_coro():
        raise CriticalError("probe failed")

    with (
        patch("bot.circuit_breaker.manager.async_get_cb_state",
              new_callable=AsyncMock,
              return_value={"state": CBState.HALF_OPEN}),
        patch("bot.circuit_breaker.manager.async_trip_circuit_breaker",
              new_callable=AsyncMock) as mock_trip,
        patch("bot.circuit_breaker.manager.async_log_event", new_callable=AsyncMock),
    ):
        with pytest.raises(CriticalError):
            await cb.execute_with_cb(Component.ORDER_EXECUTION, failing_coro())

    mock_trip.assert_called_once()
    cb._notify_fn.assert_called_once()
    assert "probe" in cb._notify_fn.call_args[0][0].lower()


# ── 9. Tidak notify jika notify_fn tidak di-set ────────────────────────────────

@pytest.mark.asyncio
async def test_no_notify_if_fn_not_set():
    cb_no_notify = CircuitBreakerManager(error_threshold=3, window_minutes=5)

    with (
        _patch_cb_state(CBState.CLOSED),
        patch("bot.circuit_breaker.manager.async_record_error", new_callable=AsyncMock),
        patch("bot.circuit_breaker.manager.async_trip_circuit_breaker", new_callable=AsyncMock),
        patch("bot.circuit_breaker.manager.async_log_event", new_callable=AsyncMock),
    ):
        # Trigger trip tanpa notify_fn — tidak boleh raise
        for i in range(3):
            await cb_no_notify.on_error(Component.SIGNAL_PARSER, f"e{i}")
        # Tidak ada exception → test passed


# ── 10. CriticalError di CLOSED → on_error dipanggil ──────────────────────────

@pytest.mark.asyncio
async def test_critical_error_in_closed_triggers_on_error(cb):
    async def bad_coro():
        raise CriticalError("auth failed")

    with (
        patch("bot.circuit_breaker.manager.async_get_cb_state",
              new_callable=AsyncMock,
              return_value={"state": CBState.CLOSED}),
        patch("bot.circuit_breaker.manager.async_record_error",
              new_callable=AsyncMock) as mock_record,
        patch("bot.circuit_breaker.manager.async_trip_circuit_breaker",
              new_callable=AsyncMock),
        patch("bot.circuit_breaker.manager.async_log_event", new_callable=AsyncMock),
    ):
        with pytest.raises(CriticalError):
            await cb.execute_with_cb(Component.BITGET_CONNECTION, bad_coro())

    mock_record.assert_called_once()


# ── 11. on_success di CLOSED reset counter ─────────────────────────────────────

@pytest.mark.asyncio
async def test_on_success_closed_resets_counter(cb):
    with (
        _patch_cb_state(CBState.CLOSED),
        patch("bot.circuit_breaker.manager.async_reset_error_count",
              new_callable=AsyncMock) as mock_rc,
    ):
        # Window kosong → reset dipanggil
        await cb.on_success(Component.TELEGRAM_LISTENER)

    mock_rc.assert_called_once_with(Component.TELEGRAM_LISTENER)


# ── 12. get_summary mengembalikan dict ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_summary(cb):
    expected = {
        "overall_healthy": True,
        "components": {},
        "tripped_count": 0,
        "tripped_components": [],
    }
    with patch(
        "bot.circuit_breaker.manager.async_get_cb_summary_for_dashboard",
        new_callable=AsyncMock,
        return_value=expected,
    ):
        result = await cb.get_summary()

    assert result["overall_healthy"] is True
    assert result["tripped_count"] == 0
