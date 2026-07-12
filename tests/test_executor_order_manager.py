"""
tests/test_executor_order_manager.py
=====================================
Unit tests Step 13 — bot/executor/order_manager.py
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.executor.order_manager import (
    OrderManagementResult,
    close_all_positions,
    close_position,
    cancel_pending_order,
    format_order_management_notification,
    set_stop_loss,
    _opposite_side,
    _hold_side,
)
from core.constants import CloseReason, Direction


# ── Pure helpers ──────────────────────────────────────────────────────────────

def test_opposite_side_long():
    assert _opposite_side(Direction.LONG) == "sell"


def test_opposite_side_short():
    assert _opposite_side(Direction.SHORT) == "buy"


def test_hold_side():
    assert _hold_side(Direction.LONG) == "long"
    assert _hold_side(Direction.SHORT) == "short"


# ── Notification tests ────────────────────────────────────────────────────────

def test_notif_set_sl_success():
    r = OrderManagementResult(
        success=True, operation="set_sl", pair="ETH/USDT:USDT", trade_id=1,
        sl_price=2900.0, sl_order_id="SL123", is_dry_run=True,
    )
    text = format_order_management_notification(r)
    assert "DRY-RUN" in text
    assert "2900" in text
    assert "ETH/USDT:USDT" in text


def test_notif_cancel_success():
    r = OrderManagementResult(
        success=True, operation="cancel_order", pair="BTC/USDT:USDT", trade_id=2,
        cancelled_order_id="ORD456", is_dry_run=False,
    )
    text = format_order_management_notification(r)
    assert "cancelled" in text.lower()
    assert "BTC/USDT:USDT" in text


def test_notif_close_success():
    r = OrderManagementResult(
        success=True, operation="close_position", pair="SOL/USDT:USDT", trade_id=3,
        closed_pnl=12.34, is_dry_run=False,
    )
    text = format_order_management_notification(r)
    assert "+12.3400" in text
    assert "SOL/USDT:USDT" in text


def test_notif_close_all_partial_fail():
    r = OrderManagementResult(
        success=False, operation="close_all",
        closed_pairs=["BTC/USDT:USDT"], failed_pairs=["ETH/USDT:USDT"],
        is_dry_run=False,
    )
    text = format_order_management_notification(r)
    assert "Gagal" in text
    assert "ETH/USDT:USDT" in text


def test_notif_failure_critical():
    r = OrderManagementResult(
        success=False, operation="set_sl", pair="XAU/USDT:USDT",
        failure_reason="critical: auth error", is_critical=True,
    )
    text = format_order_management_notification(r)
    assert "CRITICAL" in text


def test_notif_failure_transient():
    r = OrderManagementResult(
        success=False, operation="close_position", pair="ETH/USDT:USDT",
        failure_reason="transient: timeout", is_critical=False,
    )
    text = format_order_management_notification(r)
    assert "⚠️" in text


# ── set_stop_loss tests ───────────────────────────────────────────────────────

def _mock_trade(
    trade_id=1, pair="ETH/USDT:USDT", direction="long",
    position_size=0.05, entry_price=3000.0, risk_amount_usd=5.0,
):
    return {
        "id": trade_id, "pair": pair, "direction": direction,
        "position_size": position_size, "entry_price": entry_price,
        "risk_amount_usd": risk_amount_usd, "status": "open",
    }


@pytest.mark.asyncio
async def test_set_stop_loss_dry_run():
    trade = _mock_trade()
    with (
        patch("bot.executor.order_manager.async_get_trade_by_id", new_callable=AsyncMock, return_value=trade),
        patch("bot.executor.order_manager.async_update_trade_sl", new_callable=AsyncMock),
        patch("bot.executor.order_manager.async_log_event", new_callable=AsyncMock),
        patch("bot.executor.order_manager.get_rest_client", return_value=MagicMock()),
    ):
        result = await set_stop_loss(1, sl_price=2900.0, dry_run=True)

    assert result.success
    assert result.is_dry_run
    assert result.sl_price == 2900.0
    assert result.pair == "ETH/USDT:USDT"


@pytest.mark.asyncio
async def test_set_stop_loss_real_order_params_no_conflicting_keys():
    """
    Regression test: create_order() sempat dipanggil dengan 'stopLossPrice'
    DAN 'triggerPrice' di params sekaligus — ccxt bitget menolak ini dengan
    'createOrder() params can only contain one of triggerPrice, stopLossPrice,
    takeProfitPrice, trailingPercent', bikin SEMUA /setsl (dan SL otomatis
    setelah entry) gagal dengan CriticalError. Fix: hanya kirim triggerPrice.
    """
    trade = _mock_trade(direction="long", position_size=0.05)

    mock_exchange = AsyncMock()
    mock_exchange.create_order = AsyncMock(return_value={
        "id": "SL789", "symbol": "ETH/USDT:USDT",
    })
    mock_client = MagicMock()
    mock_client._get_exchange = AsyncMock(return_value=mock_exchange)

    with (
        patch("bot.executor.order_manager.async_get_trade_by_id", new_callable=AsyncMock, return_value=trade),
        patch("bot.executor.order_manager.async_update_trade_sl", new_callable=AsyncMock),
        patch("bot.executor.order_manager.async_log_event", new_callable=AsyncMock),
    ):
        result = await set_stop_loss(1, sl_price=2900.0, rest_client=mock_client, dry_run=False)

    assert result.success, f"set_stop_loss gagal: {result.failure_reason}"
    mock_exchange.create_order.assert_called_once()
    _, call_kwargs = mock_exchange.create_order.call_args
    params = call_kwargs.get("params", {})

    # Cuma boleh SATU dari empat kunci exclusive ini yang dikirim ke ccxt.
    exclusive_keys = {"triggerPrice", "stopLossPrice", "takeProfitPrice", "trailingPercent"}
    present = exclusive_keys & params.keys()
    assert len(present) == 1, (
        f"params harus punya TEPAT SATU dari {exclusive_keys}, "
        f"tapi ditemukan: {present} (params={params})"
    )
    assert params.get("triggerPrice") == 2900.0
    assert "stopLossPrice" not in params


@pytest.mark.asyncio
async def test_set_stop_loss_uses_valid_bitget_order_type():
    """
    Regression test: create_order() sempat dipanggil dengan type='stop_market'
    — ccxt bitget forward string `type` itu APA ADANYA ke field `orderType`
    di request Bitget, dan Bitget HANYA menerima orderType='market' atau
    'limit'. Kirim 'stop_market'/'stop' bikin SEMUA /setsl (manual maupun
    otomatis setelah entry fill) ditolak exchange dengan:
        {"code":"400172","msg":"The order type is illegal"}
    Sifat trigger/stop-nya ditandai lewat params.triggerPrice (bukan lewat
    nama order type) — fix: selalu kirim type='market'.
    """
    trade = _mock_trade(direction="long", position_size=0.05)

    mock_exchange = AsyncMock()
    mock_exchange.create_order = AsyncMock(return_value={
        "id": "SL790", "symbol": "ETH/USDT:USDT",
    })
    mock_client = MagicMock()
    mock_client._get_exchange = AsyncMock(return_value=mock_exchange)

    with (
        patch("bot.executor.order_manager.async_get_trade_by_id", new_callable=AsyncMock, return_value=trade),
        patch("bot.executor.order_manager.async_update_trade_sl", new_callable=AsyncMock),
        patch("bot.executor.order_manager.async_log_event", new_callable=AsyncMock),
    ):
        result = await set_stop_loss(1, sl_price=2900.0, rest_client=mock_client, dry_run=False)

    assert result.success, f"set_stop_loss gagal: {result.failure_reason}"
    mock_exchange.create_order.assert_called_once()
    _, call_kwargs = mock_exchange.create_order.call_args

    assert call_kwargs.get("type") == "market", (
        f"orderType wajib 'market' (Bitget cuma terima 'market'/'limit') — "
        f"dapat: {call_kwargs.get('type')!r}. Kirim 'stop_market'/'stop' "
        f"akan ditolak exchange dengan code 400172 'The order type is illegal'."
    )


@pytest.mark.asyncio
async def test_set_stop_loss_trade_not_found():
    with (
        patch("bot.executor.order_manager.async_get_trade_by_id", new_callable=AsyncMock, return_value=None),
        patch("bot.executor.order_manager.get_rest_client", return_value=MagicMock()),
    ):
        result = await set_stop_loss(999, sl_price=100.0, dry_run=True)

    assert not result.success
    assert "trade_not_found" in result.failure_reason


@pytest.mark.asyncio
async def test_set_stop_loss_invalid_size():
    trade = _mock_trade(position_size=0.0)
    with (
        patch("bot.executor.order_manager.async_get_trade_by_id", new_callable=AsyncMock, return_value=trade),
        patch("bot.executor.order_manager.get_rest_client", return_value=MagicMock()),
    ):
        result = await set_stop_loss(1, sl_price=2900.0, dry_run=True)

    assert not result.success
    assert "position_size" in result.failure_reason


# ── cancel_pending_order tests ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cancel_order_dry_run_with_order_id():
    trade = _mock_trade(pair="BTC/USDT:USDT")
    with (
        patch("bot.executor.order_manager.async_get_trade_by_id", new_callable=AsyncMock, return_value=trade),
        patch("bot.executor.order_manager.async_cancel_trade", new_callable=AsyncMock),
        patch("bot.executor.order_manager.async_log_event", new_callable=AsyncMock),
        patch("bot.executor.order_manager.get_rest_client", return_value=MagicMock()),
    ):
        result = await cancel_pending_order(
            1, exchange_order_id="ORDER123", dry_run=True
        )

    assert result.success
    assert result.is_dry_run
    assert result.cancelled_order_id == "ORDER123"


@pytest.mark.asyncio
async def test_cancel_order_no_open_orders_found():
    """Tidak ada open order → dianggap sudah fill/cancel, tetap sukses."""
    trade = _mock_trade(pair="SOL/USDT:USDT")
    mock_client = AsyncMock()
    mock_client.fetch_open_orders = AsyncMock(return_value=[])

    with (
        patch("bot.executor.order_manager.async_get_trade_by_id", new_callable=AsyncMock, return_value=trade),
        patch("bot.executor.order_manager.async_cancel_trade", new_callable=AsyncMock),
        patch("bot.executor.order_manager.async_log_event", new_callable=AsyncMock),
        patch("bot.executor.order_manager.get_rest_client", return_value=mock_client),
    ):
        result = await cancel_pending_order(1, dry_run=True)

    assert result.success
    assert result.cancelled_order_id is None
    assert any("tidak ada" in n.lower() for n in result.notes)


# ── close_position tests ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_close_position_dry_run_long():
    trade = _mock_trade(entry_price=3000.0, position_size=0.05)
    with (
        patch("bot.executor.order_manager.async_get_trade_by_id", new_callable=AsyncMock, return_value=trade),
        patch("bot.executor.order_manager.async_close_trade", new_callable=AsyncMock),
        patch("bot.executor.order_manager.async_log_event", new_callable=AsyncMock),
        patch("bot.executor.order_manager.get_rest_client", return_value=MagicMock()),
    ):
        result = await close_position(1, close_reason=CloseReason.MANUAL, dry_run=True)

    assert result.success
    assert result.is_dry_run
    assert result.pair == "ETH/USDT:USDT"


@pytest.mark.asyncio
async def test_close_position_trade_not_found():
    with (
        patch("bot.executor.order_manager.async_get_trade_by_id", new_callable=AsyncMock, return_value=None),
        patch("bot.executor.order_manager.get_rest_client", return_value=MagicMock()),
    ):
        result = await close_position(999, dry_run=True)

    assert not result.success
    assert "trade_not_found" in result.failure_reason


@pytest.mark.asyncio
async def test_close_position_zero_size():
    trade = _mock_trade(position_size=0.0)
    with (
        patch("bot.executor.order_manager.async_get_trade_by_id", new_callable=AsyncMock, return_value=trade),
        patch("bot.executor.order_manager.get_rest_client", return_value=MagicMock()),
    ):
        result = await close_position(1, dry_run=True)

    assert not result.success
    assert "position_size" in result.failure_reason


# ── close_all_positions tests ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_close_all_no_open_trades():
    mock_client = AsyncMock()
    mock_client.fetch_positions = AsyncMock(return_value=[])

    with (
        patch("bot.executor.order_manager.async_get_open_trades", new_callable=AsyncMock, return_value=[]),
        patch("bot.executor.order_manager.async_log_event", new_callable=AsyncMock),
        patch("bot.executor.order_manager.get_rest_client", return_value=mock_client),
    ):
        result = await close_all_positions(dry_run=True)

    assert result.success
    assert any("tidak ada" in n.lower() for n in result.notes)


@pytest.mark.asyncio
async def test_close_all_dry_run_multiple():
    trades = [
        _mock_trade(trade_id=1, pair="ETH/USDT:USDT"),
        _mock_trade(trade_id=2, pair="BTC/USDT:USDT"),
    ]
    mock_client = AsyncMock()
    mock_client.fetch_positions = AsyncMock(return_value=[])

    with (
        patch("bot.executor.order_manager.async_get_open_trades", new_callable=AsyncMock, return_value=trades),
        patch("bot.executor.order_manager.async_get_trade_by_id", new_callable=AsyncMock, side_effect=trades),
        patch("bot.executor.order_manager.async_close_trade", new_callable=AsyncMock),
        patch("bot.executor.order_manager.async_log_event", new_callable=AsyncMock),
        patch("bot.executor.order_manager.get_rest_client", return_value=mock_client),
    ):
        result = await close_all_positions(dry_run=True)

    assert result.success
    assert set(result.closed_pairs) == {"ETH/USDT:USDT", "BTC/USDT:USDT"}
    assert result.failed_pairs == []