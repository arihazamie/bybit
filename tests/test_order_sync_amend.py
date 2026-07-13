"""
tests/test_order_sync_amend.py
================================
Unit test untuk fix "cancel vs amend" di bot/executor/order_sync.py.

Bug yang difix: kalau user menggeser harga entry limit (atau trigger price
SL) manual di web/app Bitget, exchange-nya melakukan CANCEL order lama +
CREATE order baru di baliknya (bukan edit in-place) — sebelum fix ini,
event 'cancelled' untuk order lama langsung divonis sebagai "dibatalkan
beneran" oleh order_sync.py, padahal trade masih hidup dengan harga baru.

Semua test mem-patch `asyncio.sleep` (grace period) supaya tidak benar-benar
menunggu, dan mem-mock REST client + db crud + notify — tidak ada I/O nyata.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.executor import order_sync
from exchange.bitget.ws_client import OrderEvent


def _order_event(**overrides) -> OrderEvent:
    defaults = dict(
        symbol="SOL/USDT:USDT",
        order_id="OLD-1",
        status="canceled",
        side="buy",
        order_type="limit",
        price=75.511,
        average=None,
        filled=0.0,
        remaining=1.0,
        trigger_price=None,
        reduce_only=False,
        timestamp_ms=None,
        source="websocket",
    )
    defaults.update(overrides)
    return OrderEvent(**defaults)


@pytest.fixture(autouse=True)
def _no_real_sleep():
    with patch("bot.executor.order_sync.asyncio.sleep", new=AsyncMock()):
        yield


# ── Entry limit: amend (price changed) vs real cancel ──────────────────────

@pytest.mark.asyncio
async def test_entry_cancel_is_actually_amend_updates_price_not_cancel():
    trade = {"id": 6, "entry_price": 75.511}
    replacement_order = {"id": "NEW-2", "price": 76.0, "reduceOnly": False, "info": {}}

    with patch("bot.executor.order_sync.async_get_pending_trade_for_pair", AsyncMock(return_value=trade)), \
         patch("bot.executor.order_sync._find_replacement_order", AsyncMock(return_value=replacement_order)), \
         patch("bot.executor.order_sync.async_update_trade_entry", AsyncMock(return_value=True)) as mock_update_entry, \
         patch("bot.executor.order_sync.async_cancel_trade", AsyncMock(return_value=True)) as mock_cancel, \
         patch("bot.executor.order_sync.notify", AsyncMock()) as mock_notify:

        await order_sync._handle_order_cancelled(_order_event())

        mock_update_entry.assert_awaited_once_with(6, 76.0)
        mock_cancel.assert_not_awaited()
        assert "DIUBAH" in mock_notify.await_args.args[0]
        assert "DIBATALKAN" not in mock_notify.await_args.args[0]


@pytest.mark.asyncio
async def test_entry_cancel_with_no_replacement_is_real_cancel():
    trade = {"id": 6, "entry_price": 75.511}

    with patch("bot.executor.order_sync.async_get_pending_trade_for_pair", AsyncMock(return_value=trade)), \
         patch("bot.executor.order_sync._find_replacement_order", AsyncMock(return_value=None)), \
         patch("bot.executor.order_sync.async_update_trade_entry", AsyncMock()) as mock_update_entry, \
         patch("bot.executor.order_sync.async_cancel_trade", AsyncMock(return_value=True)) as mock_cancel, \
         patch("bot.executor.order_sync.notify", AsyncMock()) as mock_notify:

        await order_sync._handle_order_cancelled(_order_event())

        mock_cancel.assert_awaited_once_with(6)
        mock_update_entry.assert_not_awaited()
        assert "DIBATALKAN" in mock_notify.await_args.args[0]


@pytest.mark.asyncio
async def test_entry_replacement_same_price_within_tolerance_is_noop():
    trade = {"id": 6, "entry_price": 75.511}
    # harga "baru" secara efektif sama (selisih jauh di bawah toleransi)
    replacement_order = {"id": "NEW-2", "price": 75.512, "reduceOnly": False, "info": {}}

    with patch("bot.executor.order_sync.async_get_pending_trade_for_pair", AsyncMock(return_value=trade)), \
         patch("bot.executor.order_sync._find_replacement_order", AsyncMock(return_value=replacement_order)), \
         patch("bot.executor.order_sync.async_update_trade_entry", AsyncMock()) as mock_update_entry, \
         patch("bot.executor.order_sync.async_cancel_trade", AsyncMock()) as mock_cancel, \
         patch("bot.executor.order_sync.notify", AsyncMock()) as mock_notify:

        await order_sync._handle_order_cancelled(_order_event())

        mock_update_entry.assert_not_awaited()
        mock_cancel.assert_not_awaited()
        mock_notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_find_replacement_order_excludes_same_order_id_and_filters_type():
    rest = MagicMock()
    rest.fetch_open_orders = AsyncMock(return_value=[
        {"id": "OLD-1", "price": 75.511, "reduceOnly": False, "info": {}},  # order lama, harus diskip
        {"id": "SL-9", "price": None, "reduceOnly": True, "triggerPrice": 70.0, "info": {}},  # SL, bukan entry
        {"id": "NEW-2", "price": 76.0, "reduceOnly": False, "info": {}},  # entry pengganti yang benar
    ])
    with patch("bot.executor.order_sync.get_rest_client", return_value=rest):
        found = await order_sync._find_replacement_order(
            "SOL/USDT:USDT", "OLD-1", reduce_only=False,
        )
    assert found is not None
    assert found["id"] == "NEW-2"


# ── SL (reduce-only trigger order): amend vs real "no protection" ─────────

@pytest.mark.asyncio
async def test_sl_cancel_is_actually_amend_updates_sl_not_warning():
    trade = {"id": 6, "sl_price": 70.0}
    replacement_order = {
        "id": "SL-NEW", "reduceOnly": True, "triggerPrice": 71.5, "info": {},
    }

    with patch("bot.executor.order_sync.async_get_open_trade_for_pair", AsyncMock(return_value=trade)), \
         patch("bot.executor.order_sync._find_replacement_order", AsyncMock(return_value=replacement_order)), \
         patch("bot.executor.order_sync.async_update_trade_sl", AsyncMock(return_value=True)) as mock_update_sl, \
         patch("bot.executor.order_sync.notify", AsyncMock()) as mock_notify:

        event = _order_event(
            order_id="SL-OLD", status="canceled", reduce_only=True, trigger_price=70.0,
        )
        await order_sync._handle_sl_order_cancelled(event)

        mock_update_sl.assert_awaited_once_with(6, 71.5)
        assert "STOP LOSS DIUBAH" in mock_notify.await_args.args[0]
        assert "PERINGATAN" not in mock_notify.await_args.args[0]


@pytest.mark.asyncio
async def test_sl_cancel_with_no_replacement_warns_no_protection():
    trade = {"id": 6, "sl_price": 70.0}

    with patch("bot.executor.order_sync.async_get_open_trade_for_pair", AsyncMock(return_value=trade)), \
         patch("bot.executor.order_sync._find_replacement_order", AsyncMock(return_value=None)), \
         patch("bot.executor.order_sync.async_update_trade_sl", AsyncMock()) as mock_update_sl, \
         patch("bot.executor.order_sync.notify", AsyncMock()) as mock_notify:

        event = _order_event(
            order_id="SL-OLD", status="canceled", reduce_only=True, trigger_price=70.0,
        )
        await order_sync._handle_sl_order_cancelled(event)

        mock_update_sl.assert_not_awaited()
        assert "PERINGATAN" in mock_notify.await_args.args[0]
        assert "STOP LOSS DIBATALKAN" in mock_notify.await_args.args[0]