"""
tests/test_executor_open_position.py
=====================================
Unit tests Step 12 — bot/executor/open_position.py

Scope:
  - Pure helper functions (_ccxt_side, _to_int_leverage, _parse_fill_price)
  - format_execution_notification (kedua kasus: success & failure)
  - open_position() dengan mock rest_client (dry_run=True selalu di sini)
  - Validasi input invalid (pair kosong, risk gagal, limit tanpa price)
  - Error path: CriticalError & TransientError dari _place_order stub
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Pure helpers tidak butuh env vars — import langsung
from bot.executor.open_position import (
    ExecutionResult,
    _ccxt_side,
    _parse_fill_price,
    _to_int_leverage,
    format_execution_notification,
    open_position,
)
from core.constants import Direction, EntryType, RiskMode


# ── Fixtures ─────────────────────────────────────────────────────────────────

def _make_signal(
    pair="ETH/USDT:USDT",
    direction=Direction.LONG,
    entry_type=EntryType.LIMIT,
    entry_price=3000.0,
    stop_loss=2900.0,
    raw_text="test signal",
):
    from bot.parser.signal_parser import ParsedSignal
    from core.constants import ParseStatus

    s = ParsedSignal(raw_text=raw_text)
    s.pair_normalized = pair
    s.direction = direction
    s.entry_type = entry_type
    s.entry_price = entry_price
    s.stop_loss = stop_loss
    s.parse_status = ParseStatus.SUCCESS
    s.symbol_valid = True
    return s


def _make_risk(success=True, position_size=0.05, margin_needed=15.0,
               sl_price=2900.0, entry_price_used=3000.0, leverage_used=20.0,
               failure_reason=None):
    from bot.risk_engine.risk_engine import RiskCalculationResult

    r = RiskCalculationResult(success=success)
    r.position_size = position_size
    r.margin_needed = margin_needed
    r.sl_price = sl_price
    r.entry_price_used = entry_price_used
    r.leverage_used = leverage_used
    r.max_leverage_available = 50.0
    r.risk_amount_usd = 5.0
    r.risk_mode = RiskMode.FIXED_USD
    r.free_margin = 200.0
    r.total_equity = 500.0
    r.failure_reason = failure_reason
    return r


def _make_safety(success=True, leverage_safe=20.0, leverage_requested=50.0,
                 adjusted=False, failure_reason=None):
    from bot.leverage_engine.leverage_engine import LeverageSafetyResult

    s = LeverageSafetyResult(
        success=success,
        leverage_requested=leverage_requested,
        leverage_safe=leverage_safe,
        leverage_adjusted=adjusted,
    )
    s.projection = None
    s.even_min_leverage_unsafe = False
    s.failure_reason = failure_reason
    return s


# ── Pure helper tests ─────────────────────────────────────────────────────────

def test_ccxt_side_long():
    assert _ccxt_side(Direction.LONG) == "buy"


def test_ccxt_side_short():
    assert _ccxt_side(Direction.SHORT) == "sell"


def test_to_int_leverage_floor():
    assert _to_int_leverage(20.9) == 20
    assert _to_int_leverage(1.0) == 1
    assert _to_int_leverage(0.1) == 1  # floor to min 1


def test_parse_fill_price_average():
    assert _parse_fill_price({"average": "50100.5"}, 0) == 50100.5


def test_parse_fill_price_fallback_to_price():
    assert _parse_fill_price({"price": "3000.0"}, 0) == 3000.0


def test_parse_fill_price_fallback():
    assert _parse_fill_price({}, 9999.0) == 9999.0


def test_parse_fill_price_zero_average_uses_price():
    assert _parse_fill_price({"average": "0", "price": "3000"}, 1) == 3000.0


# ── Notification format tests ─────────────────────────────────────────────────

def test_notification_failure_critical():
    r = ExecutionResult(
        success=False, pair="BTC/USDT:USDT",
        failure_reason="order_critical: InsufficientFunds",
        is_critical=True,
    )
    text = format_execution_notification(r)
    assert "CRITICAL" in text
    assert "BTC/USDT:USDT" in text


def test_notification_failure_transient():
    r = ExecutionResult(
        success=False, pair="XAU/USDT:USDT",
        failure_reason="order_transient: Timeout",
        is_critical=False,
    )
    text = format_execution_notification(r)
    assert "⚠️" in text
    assert "Timeout" in text


def test_notification_success_dry_run():
    r = ExecutionResult(
        success=True, pair="ETH/USDT:USDT",
        trade_id=42, is_dry_run=True,
        leverage_used=20.0, leverage_adjusted=False,
        entry_price_actual=3000.0, position_size=0.05, margin_used=15.0,
    )
    text = format_execution_notification(r)
    assert "DRY-RUN" in text
    assert "ETH/USDT:USDT" in text
    assert "42" in text  # trade_id


def test_notification_leverage_adjusted():
    r = ExecutionResult(
        success=True, pair="ETH/USDT:USDT",
        trade_id=1, is_dry_run=True,
        leverage_used=10.0, leverage_adjusted=True,
        entry_price_actual=3000.0, position_size=0.05, margin_used=30.0,
        notes=["Leverage diturunkan otomatis: 50x → 10x (buffer liquidation)"],
    )
    text = format_execution_notification(r)
    assert "leverage" in text.lower() or "Leverage" in text


# ── Integration-like tests (dry_run=True, mock DB) ───────────────────────────

@pytest.mark.asyncio
async def test_open_position_dry_run_limit():
    signal = _make_signal()
    risk = _make_risk()
    safety = _make_safety()

    with (
        patch("bot.executor.open_position.async_log_event", new_callable=AsyncMock),
        patch("bot.executor.open_position.async_create_trade", new_callable=AsyncMock, return_value=1),
        patch("bot.executor.open_position.get_rest_client", return_value=MagicMock()),
    ):
        result = await open_position(signal, risk, safety, dry_run=True)

    assert result.success
    assert result.is_dry_run
    assert result.trade_id == 1
    assert result.leverage_used == 20.0
    assert result.pair == "ETH/USDT:USDT"


@pytest.mark.asyncio
async def test_open_position_dry_run_market():
    signal = _make_signal(entry_type=EntryType.MARKET, entry_price=None)
    risk = _make_risk()
    safety = _make_safety()

    with (
        patch("bot.executor.open_position.async_log_event", new_callable=AsyncMock),
        patch("bot.executor.open_position.async_create_trade", new_callable=AsyncMock, return_value=2),
        patch("bot.executor.open_position.get_rest_client", return_value=MagicMock()),
    ):
        result = await open_position(signal, risk, safety, dry_run=True)

    assert result.success
    assert result.is_dry_run


@pytest.mark.asyncio
async def test_open_position_invalid_risk():
    signal = _make_signal()
    risk = _make_risk(success=False, position_size=None, failure_reason="invalid_sl_distance")
    safety = _make_safety()

    result = await open_position(signal, risk, safety, dry_run=True)

    assert not result.success
    assert "invalid_risk_result" in result.failure_reason
    assert not result.is_critical


@pytest.mark.asyncio
async def test_open_position_invalid_safety():
    signal = _make_signal()
    risk = _make_risk()
    safety = _make_safety(success=False, failure_reason="exchange_error")

    result = await open_position(signal, risk, safety, dry_run=True)

    assert not result.success
    assert "invalid_safety_result" in result.failure_reason


@pytest.mark.asyncio
async def test_open_position_limit_missing_price():
    signal = _make_signal(entry_type=EntryType.LIMIT, entry_price=None)
    risk = _make_risk()
    safety = _make_safety()

    result = await open_position(signal, risk, safety, dry_run=True)

    assert not result.success
    assert "missing_entry_price" in result.failure_reason


@pytest.mark.asyncio
async def test_open_position_missing_pair():
    signal = _make_signal(pair="")
    signal.pair_normalized = ""
    signal.pair_raw = ""
    risk = _make_risk()
    safety = _make_safety()

    result = await open_position(signal, risk, safety, dry_run=True)

    assert not result.success
    assert "missing_pair" in result.failure_reason


@pytest.mark.asyncio
async def test_open_position_leverage_adjusted_note():
    signal = _make_signal()
    risk = _make_risk()
    safety = _make_safety(leverage_safe=10.0, leverage_requested=50.0, adjusted=True)

    with (
        patch("bot.executor.open_position.async_log_event", new_callable=AsyncMock),
        patch("bot.executor.open_position.async_create_trade", new_callable=AsyncMock, return_value=3),
        patch("bot.executor.open_position.get_rest_client", return_value=MagicMock()),
    ):
        result = await open_position(signal, risk, safety, dry_run=True)

    assert result.success
    assert result.leverage_adjusted
    assert result.leverage_used == 10.0
    assert any("Leverage diturunkan" in n for n in result.notes)
