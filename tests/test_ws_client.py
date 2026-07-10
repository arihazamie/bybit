"""
tests/test_ws_client.py
========================
Unit test untuk Step 8 — Bitget WebSocket realtime client.

Semua test menggunakan mock/fake — TIDAK ada koneksi WebSocket/REST nyata.
ccxt.pro & ccxt.async_support tidak pernah benar-benar dipanggil ke network;
exchange object di-patch dengan AsyncMock.

Cakupan test:
  1. _parse_order / _parse_position — normalisasi raw ccxt dict ke event
  2. OrderEvent.is_filled / is_cancelled, PositionEvent.is_closed
  3. _dispatch_order / _dispatch_position — callback dipanggil dengan event yang benar,
     dan exception di callback tidak menghentikan dispatch (di-catch & di-log)
  4. _watch_orders_loop / _watch_positions_loop — reconnect setelah error,
     backoff dipanggil, loop berhenti bersih saat stop()
  5. _reconcile_once — fetch_open_orders + fetch_positions dipanggil, posisi
     dengan contracts=0 di-skip
  6. _backoff_for — sesuai tabel WS_RECONNECT_BACKOFF_SECONDS lalu plateau
  7. start()/stop() — task lifecycle, double start() no-op
  8. Singleton get_ws_client / reset_ws_client

Jalankan:
    python -m unittest tests.test_ws_client -v
"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from exchange.bitget.ws_client import (
    WS_RECONNECT_BACKOFF_SECONDS,
    WS_RECONNECT_MAX_BACKOFF_SECONDS,
    BitgetWsClient,
    OrderEvent,
    PositionEvent,
    get_ws_client,
    reset_ws_client,
)


def run(coro):
    """Jalankan coroutine di event loop baru untuk test sinkron."""
    return asyncio.run(coro)


def _make_client(**kwargs) -> BitgetWsClient:
    return BitgetWsClient(
        api_key="x", api_secret="x", passphrase="x", sandbox=True, **kwargs
    )


# ── 1 & 2. Parsing & event properties ─────────────────────────────────────────

class TestParseOrder(unittest.TestCase):

    def setUp(self):
        self.client = _make_client()

    def test_parse_basic_order(self):
        raw = {
            "symbol": "BTC/USDT:USDT",
            "id": "1001",
            "status": "open",
            "side": "buy",
            "type": "limit",
            "price": 65000.0,
            "average": None,
            "filled": 0.0,
            "remaining": 0.1,
            "timestamp": 1700000000000,
            "info": {},
        }
        event = self.client._parse_order(raw, source="websocket")
        self.assertEqual(event.symbol, "BTC/USDT:USDT")
        self.assertEqual(event.order_id, "1001")
        self.assertEqual(event.status, "open")
        self.assertEqual(event.side, "buy")
        self.assertEqual(event.price, 65000.0)
        self.assertEqual(event.remaining, 0.1)
        self.assertEqual(event.source, "websocket")
        self.assertFalse(event.reduce_only)

    def test_parse_order_filled_is_filled_true(self):
        raw = {
            "symbol": "ETH/USDT:USDT", "id": "2002", "status": "closed",
            "side": "buy", "type": "market", "price": None, "average": 3200.0,
            "filled": 1.5, "remaining": 0.0, "timestamp": None, "info": {},
        }
        event = self.client._parse_order(raw, source="websocket")
        self.assertTrue(event.is_filled)
        self.assertFalse(event.is_cancelled)

    def test_parse_order_cancelled(self):
        raw = {
            "symbol": "ETH/USDT:USDT", "id": "3003", "status": "canceled",
            "side": "sell", "type": "limit", "price": 3000.0, "average": None,
            "filled": 0.0, "remaining": 1.0, "timestamp": None, "info": {},
        }
        event = self.client._parse_order(raw, source="websocket")
        self.assertTrue(event.is_cancelled)
        self.assertFalse(event.is_filled)

    def test_parse_order_trigger_price_for_sl(self):
        """Stop Loss order Bitget muncul sebagai trigger order — pastikan trigger_price terbaca."""
        raw = {
            "symbol": "STG/USDT:USDT", "id": "4004", "status": "open",
            "side": "sell", "type": "stop", "price": None, "average": None,
            "filled": 0.0, "remaining": 100.0, "triggerPrice": 0.45,
            "reduceOnly": True, "timestamp": None, "info": {},
        }
        event = self.client._parse_order(raw, source="websocket")
        self.assertEqual(event.trigger_price, 0.45)
        self.assertTrue(event.reduce_only)

    def test_parse_order_trigger_price_fallback_stop_price(self):
        raw = {
            "symbol": "STG/USDT:USDT", "id": "5005", "status": "open",
            "side": "sell", "type": "stop", "price": None, "average": None,
            "filled": 0.0, "remaining": 100.0, "stopPrice": 0.40,
            "timestamp": None, "info": {},
        }
        event = self.client._parse_order(raw, source="websocket")
        self.assertEqual(event.trigger_price, 0.40)

    def test_parse_order_id_fallback_to_info(self):
        raw = {
            "symbol": "BTC/USDT:USDT", "id": None, "status": "open",
            "side": "buy", "type": "limit", "price": 1.0, "average": None,
            "filled": 0.0, "remaining": 1.0, "timestamp": None,
            "info": {"orderId": "raw-id-999"},
        }
        event = self.client._parse_order(raw, source="reconciliation")
        self.assertEqual(event.order_id, "raw-id-999")
        self.assertEqual(event.source, "reconciliation")

    def test_parse_order_missing_filled_defaults_zero(self):
        raw = {
            "symbol": "BTC/USDT:USDT", "id": "6006", "status": "open",
            "side": "buy", "type": "limit", "price": 1.0,
            "info": {},
        }
        event = self.client._parse_order(raw, source="websocket")
        self.assertEqual(event.filled, 0.0)
        self.assertEqual(event.remaining, 0.0)


class TestParsePosition(unittest.TestCase):

    def setUp(self):
        self.client = _make_client()

    def test_parse_basic_position(self):
        raw = {
            "symbol": "BTC/USDT:USDT",
            "side": "long",
            "contracts": 0.05,
            "entryPrice": 64000.0,
            "markPrice": 64500.0,
            "liquidationPrice": 58000.0,
            "unrealizedPnl": 25.0,
            "leverage": 20.0,
            "marginMode": "cross",
            "timestamp": 1700000000000,
        }
        event = self.client._parse_position(raw, source="websocket")
        self.assertEqual(event.symbol, "BTC/USDT:USDT")
        self.assertEqual(event.side, "long")
        self.assertEqual(event.contracts, 0.05)
        self.assertEqual(event.liquidation_price, 58000.0)
        self.assertEqual(event.margin_mode, "cross")
        self.assertFalse(event.is_closed)

    def test_parse_position_closed(self):
        raw = {"symbol": "BTC/USDT:USDT", "side": "long", "contracts": 0.0}
        event = self.client._parse_position(raw, source="reconciliation")
        self.assertTrue(event.is_closed)

    def test_parse_position_missing_fields_no_crash(self):
        raw = {"symbol": "ETH/USDT:USDT"}
        event = self.client._parse_position(raw, source="websocket")
        self.assertEqual(event.contracts, 0.0)
        self.assertIsNone(event.entry_price)
        self.assertIsNone(event.liquidation_price)


# ── 3. Dispatch & callbacks ───────────────────────────────────────────────────

class TestDispatch(unittest.TestCase):

    def test_dispatch_order_calls_callback(self):
        received = []

        async def on_order(event: OrderEvent):
            received.append(event)

        client = _make_client(on_order=on_order)
        raw = {
            "symbol": "BTC/USDT:USDT", "id": "1", "status": "open",
            "side": "buy", "type": "limit", "price": 1.0, "filled": 0.0,
            "remaining": 1.0, "info": {},
        }
        run(client._dispatch_order(raw, source="websocket"))

        self.assertEqual(len(received), 1)
        self.assertEqual(received[0].symbol, "BTC/USDT:USDT")
        self.assertIsNotNone(client.last_order_event_at)

    def test_dispatch_order_no_callback_no_crash(self):
        client = _make_client(on_order=None)
        raw = {"symbol": "BTC/USDT:USDT", "id": "1", "status": "open", "info": {}}
        run(client._dispatch_order(raw, source="websocket"))  # tidak boleh raise

    def test_dispatch_order_callback_exception_is_caught(self):
        """Exception di callback user TIDAK boleh menjatuhkan loop watch_orders."""
        async def bad_callback(event: OrderEvent):
            raise RuntimeError("boom")

        client = _make_client(on_order=bad_callback)
        raw = {"symbol": "BTC/USDT:USDT", "id": "1", "status": "open", "info": {}}
        try:
            run(client._dispatch_order(raw, source="websocket"))
        except RuntimeError:
            self.fail("_dispatch_order seharusnya menangkap exception dari callback")

    def test_dispatch_position_calls_callback(self):
        received = []

        async def on_position(event: PositionEvent):
            received.append(event)

        client = _make_client(on_position=on_position)
        raw = {"symbol": "BTC/USDT:USDT", "side": "long", "contracts": 0.1}
        run(client._dispatch_position(raw, source="websocket"))

        self.assertEqual(len(received), 1)
        self.assertEqual(received[0].side, "long")
        self.assertIsNotNone(client.last_position_event_at)

    def test_dispatch_position_callback_exception_is_caught(self):
        async def bad_callback(event: PositionEvent):
            raise RuntimeError("boom")

        client = _make_client(on_position=bad_callback)
        raw = {"symbol": "BTC/USDT:USDT", "side": "long", "contracts": 0.1}
        try:
            run(client._dispatch_position(raw, source="websocket"))
        except RuntimeError:
            self.fail("_dispatch_position seharusnya menangkap exception dari callback")


# ── 4. watch loops: reconnect & clean stop ────────────────────────────────────

class TestWatchOrdersLoop(unittest.TestCase):

    def test_loop_dispatches_events_then_stops(self):
        """watch_orders() mengembalikan 1 batch event, lalu client di-stop di tengah loop."""
        client = _make_client()
        received = []

        async def on_order(event):
            received.append(event)

        client._on_order = on_order

        call_count = {"n": 0}

        async def fake_watch_orders():
            call_count["n"] += 1
            if call_count["n"] == 1:
                return [{"symbol": "BTC/USDT:USDT", "id": "1", "status": "open", "info": {}}]
            # Panggilan kedua: hentikan client supaya loop while keluar
            client._running = False
            await asyncio.sleep(0)
            return []

        fake_exchange = MagicMock()
        fake_exchange.watch_orders = AsyncMock(side_effect=fake_watch_orders)

        async def scenario():
            client._running = True
            with patch.object(client, "_get_ws_exchange", AsyncMock(return_value=fake_exchange)):
                await client._watch_orders_loop()

        run(scenario())
        self.assertEqual(len(received), 1)
        self.assertTrue(client.ws_orders_connected)

    def test_loop_reconnects_after_error(self):
        """Error pertama → backoff & reconnect; percobaan kedua sukses lalu loop dihentikan."""
        client = _make_client()
        attempts = {"n": 0}

        async def fake_watch_orders():
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise ConnectionError("ws dropped")
            client._running = False
            return []

        fake_exchange = MagicMock()
        fake_exchange.watch_orders = AsyncMock(side_effect=fake_watch_orders)

        async def scenario():
            client._running = True
            with patch.object(client, "_get_ws_exchange", AsyncMock(return_value=fake_exchange)), \
                 patch.object(client, "_backoff_for", return_value=0.001), \
                 patch.object(client, "_reset_ws_exchange", AsyncMock()) as mock_reset:
                await client._watch_orders_loop()
                return mock_reset

        mock_reset = run(scenario())
        self.assertEqual(attempts["n"], 2)
        self.assertEqual(client.orders_reconnect_count, 1)
        mock_reset.assert_called()

    def test_cancelled_error_propagates(self):
        """asyncio.CancelledError (dari task.cancel()) harus tetap propagate, bukan ditelan."""
        client = _make_client()
        client._running = True

        async def fake_watch_orders():
            raise asyncio.CancelledError()

        fake_exchange = MagicMock()
        fake_exchange.watch_orders = AsyncMock(side_effect=fake_watch_orders)

        async def scenario():
            with patch.object(client, "_get_ws_exchange", AsyncMock(return_value=fake_exchange)):
                await client._watch_orders_loop()

        with self.assertRaises(asyncio.CancelledError):
            run(scenario())


class TestWatchPositionsLoop(unittest.TestCase):

    def test_loop_dispatches_position_events(self):
        client = _make_client()
        received = []

        async def on_position(event):
            received.append(event)

        client._on_position = on_position
        call_count = {"n": 0}

        async def fake_watch_positions():
            call_count["n"] += 1
            if call_count["n"] == 1:
                return [{"symbol": "BTC/USDT:USDT", "side": "long", "contracts": 0.1}]
            client._running = False
            return []

        fake_exchange = MagicMock()
        fake_exchange.watch_positions = AsyncMock(side_effect=fake_watch_positions)

        async def scenario():
            client._running = True
            with patch.object(client, "_get_ws_exchange", AsyncMock(return_value=fake_exchange)):
                await client._watch_positions_loop()

        run(scenario())
        self.assertEqual(len(received), 1)
        self.assertTrue(client.ws_positions_connected)


# ── 5. Reconciliation ─────────────────────────────────────────────────────────

class TestReconciliation(unittest.TestCase):

    def test_reconcile_once_dispatches_orders_and_positions(self):
        client = _make_client()
        order_events = []
        position_events = []

        async def on_order(e):
            order_events.append(e)

        async def on_position(e):
            position_events.append(e)

        client._on_order = on_order
        client._on_position = on_position

        fake_orders = [{"symbol": "BTC/USDT:USDT", "id": "1", "status": "open", "info": {}}]
        fake_positions = [
            {"symbol": "BTC/USDT:USDT", "side": "long", "contracts": 0.05},
            {"symbol": "ETH/USDT:USDT", "side": "long", "contracts": 0.0},  # closed, harus di-skip
        ]

        client._rest.fetch_open_orders = AsyncMock(return_value=fake_orders)
        client._rest.fetch_positions = AsyncMock(return_value=fake_positions)

        run(client._reconcile_once())

        self.assertEqual(len(order_events), 1)
        self.assertEqual(len(position_events), 1)  # posisi contracts=0 di-skip
        self.assertEqual(position_events[0].symbol, "BTC/USDT:USDT")

    def test_reconciliation_loop_runs_and_records_timestamp(self):
        client = _make_client(reconcile_interval=0.001)
        client._rest.fetch_open_orders = AsyncMock(return_value=[])
        client._rest.fetch_positions = AsyncMock(return_value=[])

        async def scenario():
            client._running = True

            async def stop_after_one_cycle():
                await asyncio.sleep(0.01)
                client._running = False

            await asyncio.gather(
                client._reconciliation_loop(),
                stop_after_one_cycle(),
            )

        run(scenario())
        self.assertIsNotNone(client.last_reconcile_at)
        self.assertIsNone(client.last_reconcile_error)

    def test_reconciliation_loop_records_error_without_crashing(self):
        client = _make_client(reconcile_interval=0.001)
        client._rest.fetch_open_orders = AsyncMock(side_effect=RuntimeError("rest down"))

        async def scenario():
            client._running = True

            async def stop_after_one_cycle():
                await asyncio.sleep(0.01)
                client._running = False

            await asyncio.gather(
                client._reconciliation_loop(),
                stop_after_one_cycle(),
            )

        run(scenario())  # tidak boleh raise
        self.assertIsNotNone(client.last_reconcile_error)
        self.assertIn("rest down", client.last_reconcile_error)


# ── 6. Backoff ────────────────────────────────────────────────────────────────

class TestBackoff(unittest.TestCase):

    def test_backoff_follows_table(self):
        for i, expected in enumerate(WS_RECONNECT_BACKOFF_SECONDS):
            self.assertEqual(BitgetWsClient._backoff_for(i), expected)

    def test_backoff_plateaus_after_table_exhausted(self):
        n = len(WS_RECONNECT_BACKOFF_SECONDS)
        self.assertEqual(BitgetWsClient._backoff_for(n), WS_RECONNECT_MAX_BACKOFF_SECONDS)
        self.assertEqual(BitgetWsClient._backoff_for(n + 10), WS_RECONNECT_MAX_BACKOFF_SECONDS)


# ── 7. start()/stop() lifecycle ───────────────────────────────────────────────

class TestLifecycle(unittest.TestCase):

    def test_start_creates_three_tasks(self):
        client = _make_client()

        async def scenario():
            with patch.object(client, "_watch_orders_loop", AsyncMock(return_value=None)), \
                 patch.object(client, "_watch_positions_loop", AsyncMock(return_value=None)), \
                 patch.object(client, "_reconciliation_loop", AsyncMock(return_value=None)):
                await client.start()
                self.assertTrue(client._running)
                self.assertEqual(len(client._tasks), 3)
                await client.stop()
                self.assertFalse(client._running)
                self.assertEqual(client._tasks, [])

        run(scenario())

    def test_double_start_is_noop(self):
        client = _make_client()

        async def scenario():
            with patch.object(client, "_watch_orders_loop", AsyncMock(return_value=None)), \
                 patch.object(client, "_watch_positions_loop", AsyncMock(return_value=None)), \
                 patch.object(client, "_reconciliation_loop", AsyncMock(return_value=None)):
                await client.start()
                first_tasks = list(client._tasks)
                await client.start()  # no-op, tidak bikin task baru
                self.assertEqual(client._tasks, first_tasks)
                await client.stop()

        run(scenario())

    def test_stop_without_start_is_safe(self):
        client = _make_client()
        run(client.stop())  # tidak boleh raise


# ── 8. Singleton ───────────────────────────────────────────────────────────────

class TestSingleton(unittest.TestCase):

    def test_get_ws_client_same_instance(self):
        c1 = get_ws_client()
        c2 = get_ws_client()
        self.assertIs(c1, c2)

    def test_reset_creates_new_instance(self):
        c1 = get_ws_client()
        run(reset_ws_client())
        c2 = get_ws_client()
        self.assertIsNot(c1, c2)

    def tearDown(self):
        run(reset_ws_client())


if __name__ == "__main__":
    unittest.main(verbosity=2)
