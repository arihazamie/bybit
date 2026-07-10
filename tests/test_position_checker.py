"""
tests/test_position_checker.py
=================================
Unit test untuk Step 11 — Position checker module (bagian 5 prompt.md).

Semua test OFFLINE — tidak ada koneksi nyata ke Bitget atau database asli.
Orchestrator async (`check_position_condition`) di-test dengan FakeRestClient
+ patch fungsi DB/settings (`async_get_open_trade_for_pair`,
`async_get_position_conflict_mode`), konsisten dengan pola
tests/test_risk_engine.py dan tests/test_leverage_engine.py.

Cakupan test:
  1. _classify_condition — none / open_position / pending_order / open_and_pending
  2. resolve_conflict_action — semua kombinasi condition x conflict_mode
  3. get_conflict_action_options — opsi sesuai kondisi
  4. _parse_live_position / _parse_live_pending_order — parsing raw ccxt dict
  5. check_position_condition — orchestrator penuh: no conflict, open position,
     pending order, conflict_mode skip/ask/add/replace, untracked in DB,
     error exchange (critical & transient)
  6. format_position_check_notification — teks notifikasi tiap kondisi

Jalankan:
    python -m unittest tests.test_position_checker -v
"""

from __future__ import annotations

import asyncio
import unittest
from typing import Any, Dict, List, Optional
from unittest.mock import patch

from bot.position_checker.position_checker import (
    LivePendingOrderInfo,
    LivePositionInfo,
    PositionCheckResult,
    _classify_condition,
    _parse_live_pending_order,
    _parse_live_position,
    check_position_condition,
    format_position_check_notification,
    get_conflict_action_options,
    resolve_conflict_action,
)
from core.constants import Direction, PositionAction, PositionCondition
from exchange.bitget.retry import CriticalError, TransientError


def run(coro):
    """Jalankan coroutine di event loop baru untuk test sinkron."""
    return asyncio.run(coro)


# ── Fake exchange client (dependency injection — tanpa network) ─────────

class FakeRestClient:
    """
    Pengganti BitgetRestClient untuk unit test — semua method async, tidak
    ada network call sama sekali.
    """

    def __init__(
        self,
        *,
        positions: Optional[List[Dict[str, Any]]] = None,
        open_orders: Optional[List[Dict[str, Any]]] = None,
        fail_positions: Optional[Exception] = None,
        fail_orders: Optional[Exception] = None,
    ) -> None:
        self._positions = positions or []
        self._open_orders = open_orders or []
        self._fail_positions = fail_positions
        self._fail_orders = fail_orders

        self.fetch_positions_calls = 0
        self.fetch_open_orders_calls = 0

    async def fetch_positions(self, symbols: Optional[List[str]] = None):
        self.fetch_positions_calls += 1
        if self._fail_positions:
            raise self._fail_positions
        return self._positions

    async def fetch_open_orders(self, symbol: Optional[str] = None):
        self.fetch_open_orders_calls += 1
        if self._fail_orders:
            raise self._fail_orders
        return self._open_orders


def _patch_db_and_settings(
    db_trade: Optional[dict] = None,
    conflict_mode: str = "ask",
):
    """
    Helper: patch async_get_open_trade_for_pair & async_get_position_conflict_mode
    di namespace bot.position_checker.position_checker (lokasi import).
    """
    async def fake_get_open_trade(pair):
        return db_trade

    async def fake_conflict_mode():
        return conflict_mode

    return (
        patch(
            "bot.position_checker.position_checker.async_get_open_trade_for_pair",
            side_effect=fake_get_open_trade,
        ),
        patch(
            "bot.position_checker.position_checker.async_get_position_conflict_mode",
            side_effect=fake_conflict_mode,
        ),
    )


def _raw_position(symbol="BTC/USDT:USDT", side="long", contracts=1.5,
                   entry_price=50000.0, upnl=12.5, leverage=20):
    return {
        "symbol": symbol,
        "side": side,
        "contracts": contracts,
        "entryPrice": entry_price,
        "unrealizedPnl": upnl,
        "leverage": leverage,
    }


def _raw_order(symbol="BTC/USDT:USDT", side="buy", price=49000.0,
                amount=1.0, order_id="ord-1", order_type="limit", timestamp=1000):
    return {
        "symbol": symbol,
        "side": side,
        "price": price,
        "amount": amount,
        "id": order_id,
        "type": order_type,
        "timestamp": timestamp,
    }


# ── 1. _classify_condition ───────────────────────────────────────────────

class TestClassifyCondition(unittest.TestCase):
    def test_none(self):
        self.assertEqual(_classify_condition(False, False), PositionCondition.NONE)

    def test_open_position_only(self):
        self.assertEqual(
            _classify_condition(True, False), PositionCondition.OPEN_POSITION
        )

    def test_pending_order_only(self):
        self.assertEqual(
            _classify_condition(False, True), PositionCondition.PENDING_ORDER
        )

    def test_open_and_pending(self):
        self.assertEqual(
            _classify_condition(True, True), PositionCondition.OPEN_AND_PENDING
        )


# ── 2. resolve_conflict_action ───────────────────────────────────────────

class TestResolveConflictAction(unittest.TestCase):
    def test_none_condition_always_proceed(self):
        for mode in ("skip", "ask", "add", "replace", "weird_unknown"):
            self.assertEqual(
                resolve_conflict_action(PositionCondition.NONE, mode),
                PositionAction.PROCEED,
            )

    def test_skip_mode(self):
        self.assertEqual(
            resolve_conflict_action(PositionCondition.OPEN_POSITION, "skip"),
            PositionAction.SKIP,
        )

    def test_ask_mode_default(self):
        self.assertEqual(
            resolve_conflict_action(PositionCondition.PENDING_ORDER, "ask"),
            PositionAction.ASK_CONFIRMATION,
        )

    def test_add_mode(self):
        self.assertEqual(
            resolve_conflict_action(PositionCondition.OPEN_AND_PENDING, "add"),
            PositionAction.ADD,
        )

    def test_replace_mode(self):
        self.assertEqual(
            resolve_conflict_action(PositionCondition.OPEN_POSITION, "replace"),
            PositionAction.REPLACE,
        )

    def test_unknown_mode_falls_back_to_ask(self):
        self.assertEqual(
            resolve_conflict_action(PositionCondition.OPEN_POSITION, "garbage_mode"),
            PositionAction.ASK_CONFIRMATION,
        )

    def test_mode_case_and_whitespace_insensitive(self):
        self.assertEqual(
            resolve_conflict_action(PositionCondition.OPEN_POSITION, "  SKIP  "),
            PositionAction.SKIP,
        )


# ── 3. get_conflict_action_options ───────────────────────────────────────

class TestConflictActionOptions(unittest.TestCase):
    def test_pending_order_options(self):
        options = get_conflict_action_options(PositionCondition.PENDING_ORDER)
        actions = {o.action for o in options}
        self.assertIn(PositionAction.REPLACE, actions)
        self.assertIn(PositionAction.SKIP, actions)
        self.assertIn(PositionAction.ADD, actions)

    def test_open_position_options(self):
        options = get_conflict_action_options(PositionCondition.OPEN_POSITION)
        actions = {o.action for o in options}
        self.assertIn(PositionAction.ADD, actions)
        self.assertIn(PositionAction.SKIP, actions)
        self.assertIn(PositionAction.REPLACE, actions)

    def test_options_have_labels_and_descriptions(self):
        options = get_conflict_action_options(PositionCondition.OPEN_POSITION)
        for opt in options:
            self.assertTrue(opt.label)
            self.assertTrue(opt.description)


# ── 4. Parsing raw ccxt dicts ────────────────────────────────────────────

class TestParsing(unittest.TestCase):
    def test_parse_live_position_long(self):
        parsed = _parse_live_position(_raw_position(side="long", contracts=2.0))
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.direction, Direction.LONG)
        self.assertEqual(parsed.contracts, 2.0)
        self.assertTrue(parsed.is_long)

    def test_parse_live_position_short(self):
        parsed = _parse_live_position(_raw_position(side="short", contracts=-3.0))
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.direction, Direction.SHORT)
        # contracts dinormalisasi ke absolute value
        self.assertEqual(parsed.contracts, 3.0)

    def test_parse_live_position_zero_contracts_returns_none(self):
        parsed = _parse_live_position(_raw_position(contracts=0))
        self.assertIsNone(parsed)

    def test_parse_live_pending_order_buy(self):
        parsed = _parse_live_pending_order(_raw_order(side="buy"))
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.direction, Direction.LONG)
        self.assertEqual(parsed.order_id, "ord-1")

    def test_parse_live_pending_order_sell(self):
        parsed = _parse_live_pending_order(_raw_order(side="sell"))
        self.assertEqual(parsed.direction, Direction.SHORT)


# ── 5. check_position_condition — orchestrator penuh ─────────────────────

class TestCheckPositionCondition(unittest.TestCase):
    PAIR = "BTC/USDT:USDT"

    def test_no_conflict_proceed(self):
        client = FakeRestClient(positions=[], open_orders=[])
        p_db, p_mode = _patch_db_and_settings(db_trade=None, conflict_mode="ask")
        with p_db, p_mode:
            result = run(check_position_condition(self.PAIR, rest_client=client))

        self.assertTrue(result.success)
        self.assertEqual(result.condition, PositionCondition.NONE)
        self.assertEqual(result.recommended_action, PositionAction.PROCEED)
        self.assertIsNone(result.live_position)
        self.assertIsNone(result.live_pending_order)

    def test_open_position_ask_mode(self):
        client = FakeRestClient(
            positions=[_raw_position(symbol=self.PAIR)],
            open_orders=[],
        )
        db_trade = {"id": 42, "pair": self.PAIR, "sl_price": 48000.0,
                    "tp_price": None, "source_analyst": "Faith"}
        p_db, p_mode = _patch_db_and_settings(db_trade=db_trade, conflict_mode="ask")
        with p_db, p_mode:
            result = run(check_position_condition(self.PAIR, rest_client=client))

        self.assertTrue(result.success)
        self.assertEqual(result.condition, PositionCondition.OPEN_POSITION)
        self.assertEqual(result.recommended_action, PositionAction.ASK_CONFIRMATION)
        self.assertIsNotNone(result.live_position)
        self.assertEqual(result.db_trade, db_trade)
        self.assertFalse(result.untracked_in_db)

    def test_pending_order_skip_mode(self):
        client = FakeRestClient(
            positions=[],
            open_orders=[_raw_order(symbol=self.PAIR)],
        )
        p_db, p_mode = _patch_db_and_settings(db_trade=None, conflict_mode="skip")
        with p_db, p_mode:
            result = run(check_position_condition(self.PAIR, rest_client=client))

        self.assertEqual(result.condition, PositionCondition.PENDING_ORDER)
        self.assertEqual(result.recommended_action, PositionAction.SKIP)
        self.assertIsNotNone(result.live_pending_order)

    def test_open_and_pending_add_mode(self):
        client = FakeRestClient(
            positions=[_raw_position(symbol=self.PAIR)],
            open_orders=[_raw_order(symbol=self.PAIR)],
        )
        p_db, p_mode = _patch_db_and_settings(db_trade=None, conflict_mode="add")
        with p_db, p_mode:
            result = run(check_position_condition(self.PAIR, rest_client=client))

        self.assertEqual(result.condition, PositionCondition.OPEN_AND_PENDING)
        self.assertEqual(result.recommended_action, PositionAction.ADD)

    def test_replace_mode(self):
        client = FakeRestClient(
            positions=[_raw_position(symbol=self.PAIR)],
            open_orders=[],
        )
        p_db, p_mode = _patch_db_and_settings(db_trade=None, conflict_mode="replace")
        with p_db, p_mode:
            result = run(check_position_condition(self.PAIR, rest_client=client))

        self.assertEqual(result.recommended_action, PositionAction.REPLACE)

    def test_untracked_in_db_flag(self):
        """Posisi terdeteksi di exchange tapi tidak ada record DB → flagged."""
        client = FakeRestClient(
            positions=[_raw_position(symbol=self.PAIR)],
            open_orders=[],
        )
        p_db, p_mode = _patch_db_and_settings(db_trade=None, conflict_mode="ask")
        with p_db, p_mode:
            result = run(check_position_condition(self.PAIR, rest_client=client))

        self.assertTrue(result.untracked_in_db)
        self.assertTrue(any("manual" in n.lower() for n in result.notes))

    def test_ignores_position_for_other_symbol(self):
        """Posisi untuk simbol lain tidak boleh dianggap konflik untuk pair ini."""
        client = FakeRestClient(
            positions=[_raw_position(symbol="ETH/USDT:USDT")],
            open_orders=[],
        )
        p_db, p_mode = _patch_db_and_settings(db_trade=None, conflict_mode="ask")
        with p_db, p_mode:
            result = run(check_position_condition(self.PAIR, rest_client=client))

        self.assertEqual(result.condition, PositionCondition.NONE)
        self.assertEqual(result.recommended_action, PositionAction.PROCEED)

    def test_critical_error_from_exchange(self):
        client = FakeRestClient(fail_positions=CriticalError("auth gagal"))
        p_db, p_mode = _patch_db_and_settings(db_trade=None, conflict_mode="ask")
        with p_db, p_mode:
            result = run(check_position_condition(self.PAIR, rest_client=client))

        self.assertFalse(result.success)
        self.assertIn("exchange_error", result.failure_reason)

    def test_transient_error_from_exchange(self):
        client = FakeRestClient(fail_orders=None, fail_positions=TransientError("timeout"))
        p_db, p_mode = _patch_db_and_settings(db_trade=None, conflict_mode="ask")
        with p_db, p_mode:
            result = run(check_position_condition(self.PAIR, rest_client=client))

        self.assertFalse(result.success)
        self.assertIn("transient_error_exhausted", result.failure_reason)

    def test_db_lookup_failure_does_not_crash(self):
        """Kegagalan query DB lokal tidak boleh menggagalkan check secara total —
        tetap pakai data live exchange, hanya catat di notes."""
        client = FakeRestClient(positions=[], open_orders=[])

        async def fake_get_open_trade_fail(pair):
            raise RuntimeError("db locked")

        async def fake_conflict_mode():
            return "ask"

        with patch(
            "bot.position_checker.position_checker.async_get_open_trade_for_pair",
            side_effect=fake_get_open_trade_fail,
        ), patch(
            "bot.position_checker.position_checker.async_get_position_conflict_mode",
            side_effect=fake_conflict_mode,
        ):
            result = run(check_position_condition(self.PAIR, rest_client=client))

        self.assertTrue(result.success)
        self.assertTrue(any("db_lookup_failed" in n for n in result.notes))

    def test_settings_lookup_failure_falls_back_to_ask(self):
        client = FakeRestClient(
            positions=[_raw_position(symbol=self.PAIR)],
            open_orders=[],
        )

        async def fake_get_open_trade(pair):
            return None

        async def fake_conflict_mode_fail():
            raise RuntimeError("settings table missing")

        with patch(
            "bot.position_checker.position_checker.async_get_open_trade_for_pair",
            side_effect=fake_get_open_trade,
        ), patch(
            "bot.position_checker.position_checker.async_get_position_conflict_mode",
            side_effect=fake_conflict_mode_fail,
        ):
            result = run(check_position_condition(self.PAIR, rest_client=client))

        self.assertTrue(result.success)
        self.assertEqual(result.conflict_mode, "ask")
        self.assertEqual(result.recommended_action, PositionAction.ASK_CONFIRMATION)

    def test_multiple_pending_orders_picks_latest(self):
        client = FakeRestClient(
            positions=[],
            open_orders=[
                _raw_order(symbol=self.PAIR, order_id="old", timestamp=100),
                _raw_order(symbol=self.PAIR, order_id="new", timestamp=999),
            ],
        )
        p_db, p_mode = _patch_db_and_settings(db_trade=None, conflict_mode="ask")
        with p_db, p_mode:
            result = run(check_position_condition(self.PAIR, rest_client=client))

        self.assertEqual(result.live_pending_order.order_id, "new")


# ── 6. format_position_check_notification ────────────────────────────────

class TestFormatNotification(unittest.TestCase):
    def test_no_conflict_text(self):
        result = PositionCheckResult(
            success=True, pair="BTC/USDT:USDT", condition=PositionCondition.NONE,
            recommended_action=PositionAction.PROCEED,
        )
        text = format_position_check_notification(result)
        self.assertIn("tidak ada posisi", text.lower())

    def test_failure_text(self):
        result = PositionCheckResult(
            success=False, pair="BTC/USDT:USDT", failure_reason="exchange_error: timeout",
        )
        text = format_position_check_notification(result)
        self.assertIn("Gagal", text)
        self.assertIn("TIDAK dieksekusi", text)

    def test_open_position_ask_text_includes_options(self):
        live_pos = LivePositionInfo(
            symbol="BTC/USDT:USDT", direction=Direction.LONG, contracts=1.0,
            entry_price=50000.0, unrealized_pnl=5.0,
        )
        result = PositionCheckResult(
            success=True, pair="BTC/USDT:USDT",
            condition=PositionCondition.OPEN_POSITION,
            conflict_mode="ask",
            recommended_action=PositionAction.ASK_CONFIRMATION,
            live_position=live_pos,
        )
        text = format_position_check_notification(result)
        self.assertIn("BTC/USDT:USDT", text)
        self.assertIn("Tambah", text)

    def test_pending_order_text(self):
        live_order = LivePendingOrderInfo(
            symbol="BTC/USDT:USDT", order_id="ord-9", direction=Direction.LONG,
            price=49000.0, amount=1.0, order_type="limit",
        )
        result = PositionCheckResult(
            success=True, pair="BTC/USDT:USDT",
            condition=PositionCondition.PENDING_ORDER,
            conflict_mode="replace",
            recommended_action=PositionAction.REPLACE,
            live_pending_order=live_order,
        )
        text = format_position_check_notification(result)
        self.assertIn("Pending order", text)
        self.assertIn("REPLACE", text.upper())


if __name__ == "__main__":
    unittest.main()
