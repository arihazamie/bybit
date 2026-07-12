"""
tests/test_risk_engine.py
===========================
Unit test untuk Step 9 — Risk & margin engine (bagian 4 prompt.md).

Semua test OFFLINE — tidak ada koneksi nyata ke Bitget. Orchestrator async
(`calculate_trade_risk`) di-test dengan FakeRestClient + patch fungsi
settings (`async_get_risk_amount_config`, `async_get_leverage_cap`) supaya
tidak butuh .env atau database asli.

Cakupan test:
  1. calculate_risk_amount — mode Percent & Fixed USD (bagian 4.1)
  2. calculate_sl_distance — jarak SL valid & edge case
  3. calculate_position_size — SL dekat vs jauh (bagian 4.4)
  4. calculate_margin_needed — leverage berbeda-beda (bagian 4.4)
  5. resolve_leverage_used — default max vs cap manual user (bagian 4.2)
  6. calculate_trade_risk — orchestrator penuh: sukses, insufficient margin,
     leverage cap, market order tanpa harga eksplisit, SL=entry, error exchange
  7. RiskCalculationResult.recompute_margin — dipakai Step 10 nanti
  8. format_risk_notification — notifikasi membedakan max_loss vs margin

Jalankan:
    python -m unittest tests.test_risk_engine -v
"""

from __future__ import annotations

import asyncio
import unittest
from typing import Optional
from unittest.mock import patch

from bot.risk_engine.risk_engine import (
    RiskCalculationResult,
    calculate_margin_needed,
    calculate_position_size,
    calculate_risk_amount,
    calculate_sl_distance,
    calculate_trade_risk,
    format_risk_notification,
    resolve_leverage_used,
)
from core.constants import Direction, EntryType, RiskMode
from exchange.bitget.retry import CriticalError, TransientError
from exchange.bitget.rest_client import BalanceInfo


def run(coro):
    """Jalankan coroutine di event loop baru untuk test sinkron."""
    return asyncio.run(coro)


# ── Fake exchange client (dependency injection — tanpa network) ─────────

class FakeRestClient:
    """
    Pengganti BitgetRestClient untuk unit test — semua method async, tidak
    ada network call sama sekali. Nilai dikontrol penuh dari test case.
    """

    def __init__(
        self,
        *,
        total_equity: float = 1000.0,
        free_margin: float = 1000.0,
        max_leverage: float = 20.0,
        ticker_price: Optional[float] = None,
        fail_balance: Optional[Exception] = None,
        fail_leverage: Optional[Exception] = None,
        fail_ticker: Optional[Exception] = None,
    ) -> None:
        self._total_equity = total_equity
        self._free_margin = free_margin
        self._max_leverage = max_leverage
        self._ticker_price = ticker_price
        self._fail_balance = fail_balance
        self._fail_leverage = fail_leverage
        self._fail_ticker = fail_ticker

        self.fetch_balance_calls = 0
        self.get_max_leverage_calls = 0
        self.fetch_ticker_price_calls = 0

    async def fetch_balance(self) -> BalanceInfo:
        self.fetch_balance_calls += 1
        if self._fail_balance:
            raise self._fail_balance
        return BalanceInfo(
            total_equity=self._total_equity,
            free_margin=self._free_margin,
            wallet_balance=self._total_equity,
            unrealized_pnl=0.0,
        )

    async def get_max_leverage(self, symbol: str) -> float:
        self.get_max_leverage_calls += 1
        if self._fail_leverage:
            raise self._fail_leverage
        return self._max_leverage

    async def fetch_ticker_price(self, symbol: str) -> float:
        self.fetch_ticker_price_calls += 1
        if self._fail_ticker:
            raise self._fail_ticker
        if self._ticker_price is None:
            raise AssertionError("fetch_ticker_price dipanggil tapi ticker_price tidak diset di test")
        return self._ticker_price


def _patch_settings(risk_mode: str, risk_value: float, leverage_cap: Optional[float] = None):
    """
    Helper: patch async_get_risk_amount_config & async_get_leverage_cap di
    namespace bot.risk_engine.risk_engine (lokasi import, bukan lokasi asli
    di db.crud.settings — konsisten dengan cara test_rest_client.py mem-patch
    `exchange.bitget.rest_client.settings`).
    """
    async def fake_risk_config():
        return (risk_mode, risk_value)

    async def fake_leverage_cap(pair=None):
        return leverage_cap

    return (
        patch("bot.risk_engine.risk_engine.async_get_risk_amount_config", side_effect=fake_risk_config),
        patch("bot.risk_engine.risk_engine.async_get_leverage_cap", side_effect=fake_leverage_cap),
    )


# ── 1. calculate_risk_amount ─────────────────────────────────────────────

class TestCalculateRiskAmount(unittest.TestCase):

    def test_percent_mode_basic(self):
        # 1% dari 1000 = 10
        self.assertAlmostEqual(
            calculate_risk_amount(RiskMode.PERCENT, 1.0, total_balance=1000.0), 10.0
        )

    def test_percent_mode_different_percent(self):
        # 2.5% dari 2000 = 50
        self.assertAlmostEqual(
            calculate_risk_amount(RiskMode.PERCENT, 2.5, total_balance=2000.0), 50.0
        )

    def test_percent_mode_missing_balance_raises(self):
        with self.assertRaises(ValueError):
            calculate_risk_amount(RiskMode.PERCENT, 1.0, total_balance=None)

    def test_percent_mode_zero_balance_raises(self):
        with self.assertRaises(ValueError):
            calculate_risk_amount(RiskMode.PERCENT, 1.0, total_balance=0.0)

    def test_fixed_usd_mode_ignores_balance(self):
        """Fixed USD HARUS tetap — tidak bergantung balance sama sekali (bagian 4.1)."""
        amount_no_balance = calculate_risk_amount(RiskMode.FIXED_USD, 5.0)
        amount_with_huge_balance = calculate_risk_amount(
            RiskMode.FIXED_USD, 5.0, total_balance=999_999.0
        )
        self.assertEqual(amount_no_balance, 5.0)
        self.assertEqual(amount_with_huge_balance, 5.0)

    def test_zero_or_negative_risk_value_raises(self):
        with self.assertRaises(ValueError):
            calculate_risk_amount(RiskMode.FIXED_USD, 0.0)
        with self.assertRaises(ValueError):
            calculate_risk_amount(RiskMode.PERCENT, -1.0, total_balance=1000.0)

    def test_unknown_mode_raises(self):
        with self.assertRaises(ValueError):
            calculate_risk_amount("not_a_real_mode", 1.0, total_balance=1000.0)


# ── 2. calculate_sl_distance ─────────────────────────────────────────────

class TestCalculateSlDistance(unittest.TestCase):

    def test_long_sl_below_entry(self):
        self.assertAlmostEqual(calculate_sl_distance(100.0, 95.0), 5.0)

    def test_short_sl_above_entry(self):
        """Short: SL di atas entry — jarak tetap absolut, bukan negatif."""
        self.assertAlmostEqual(calculate_sl_distance(100.0, 105.0), 5.0)

    def test_entry_equals_sl_raises(self):
        with self.assertRaises(ValueError):
            calculate_sl_distance(100.0, 100.0)

    def test_negative_entry_raises(self):
        with self.assertRaises(ValueError):
            calculate_sl_distance(-10.0, 5.0)

    def test_zero_sl_raises(self):
        with self.assertRaises(ValueError):
            calculate_sl_distance(100.0, 0.0)

    def test_small_decimal_distance(self):
        """Pair dengan harga kecil (mis. altcoin sen-an) tetap presisi."""
        self.assertAlmostEqual(calculate_sl_distance(0.05123, 0.04900), 0.00223, places=5)


# ── 3. calculate_position_size ───────────────────────────────────────────

class TestCalculatePositionSize(unittest.TestCase):

    def test_basic(self):
        # risk 10 USD, jarak SL 5 → 2 unit
        self.assertAlmostEqual(calculate_position_size(10.0, 5.0), 2.0)

    def test_sl_close_gives_larger_position(self):
        """SL dekat (jarak kecil) → position_size LEBIH BESAR untuk risk_amount sama."""
        far = calculate_position_size(10.0, sl_distance=10.0)
        close = calculate_position_size(10.0, sl_distance=1.0)
        self.assertGreater(close, far)

    def test_sl_far_gives_smaller_position(self):
        """SL jauh (jarak besar) → position_size LEBIH KECIL — sesuai bagian 4.4."""
        result = calculate_position_size(risk_amount=20.0, sl_distance=100.0)
        self.assertAlmostEqual(result, 0.2)

    def test_zero_risk_amount_raises(self):
        with self.assertRaises(ValueError):
            calculate_position_size(0.0, 5.0)

    def test_zero_sl_distance_raises(self):
        with self.assertRaises(ValueError):
            calculate_position_size(10.0, 0.0)

    def test_negative_values_raise(self):
        with self.assertRaises(ValueError):
            calculate_position_size(-5.0, 5.0)
        with self.assertRaises(ValueError):
            calculate_position_size(5.0, -5.0)


# ── 4. calculate_margin_needed ───────────────────────────────────────────

class TestCalculateMarginNeeded(unittest.TestCase):

    def test_basic(self):
        # position_size=2, entry=100, leverage=10 → (2*100)/10 = 20
        self.assertAlmostEqual(calculate_margin_needed(2.0, 100.0, 10.0), 20.0)

    def test_higher_leverage_lower_margin(self):
        """Leverage lebih tinggi → margin lebih kecil, position_size SAMA."""
        low_lev = calculate_margin_needed(position_size=2.0, entry_price=100.0, leverage_used=5.0)
        high_lev = calculate_margin_needed(position_size=2.0, entry_price=100.0, leverage_used=50.0)
        self.assertGreater(low_lev, high_lev)
        self.assertAlmostEqual(low_lev, 40.0)
        self.assertAlmostEqual(high_lev, 4.0)

    def test_margin_varies_but_notional_constant(self):
        """
        Bagian 4.4: margin BERBEDA tiap trade tergantung leverage, tapi
        notional (position_size * entry_price) harus tetap sama untuk
        position_size & entry_price yang sama — leverage HANYA mengubah
        margin, bukan exposure notional.
        """
        notional = 2.0 * 100.0
        for lev in (1.0, 5.0, 10.0, 25.0, 50.0, 125.0):
            margin = calculate_margin_needed(2.0, 100.0, lev)
            self.assertAlmostEqual(margin * lev, notional)

    def test_zero_leverage_raises(self):
        with self.assertRaises(ValueError):
            calculate_margin_needed(2.0, 100.0, 0.0)

    def test_negative_values_raise(self):
        with self.assertRaises(ValueError):
            calculate_margin_needed(-2.0, 100.0, 10.0)
        with self.assertRaises(ValueError):
            calculate_margin_needed(2.0, -100.0, 10.0)


# ── 5. resolve_leverage_used ─────────────────────────────────────────────

class TestResolveLeverageUsed(unittest.TestCase):

    def test_no_cap_uses_max(self):
        leverage, capped = resolve_leverage_used(max_leverage_available=50.0, leverage_cap=None)
        self.assertEqual(leverage, 50.0)
        self.assertFalse(capped)

    def test_cap_below_max_is_applied(self):
        leverage, capped = resolve_leverage_used(max_leverage_available=125.0, leverage_cap=20.0)
        self.assertEqual(leverage, 20.0)
        self.assertTrue(capped)

    def test_cap_above_max_is_ignored(self):
        """/setleverage tidak boleh melebihi max asli exchange (bagian 6)."""
        leverage, capped = resolve_leverage_used(max_leverage_available=20.0, leverage_cap=999.0)
        self.assertEqual(leverage, 20.0)
        self.assertFalse(capped)

    def test_cap_equal_to_max_not_marked_as_capped(self):
        leverage, capped = resolve_leverage_used(max_leverage_available=20.0, leverage_cap=20.0)
        self.assertEqual(leverage, 20.0)
        self.assertFalse(capped)

    def test_zero_or_negative_cap_ignored(self):
        leverage, capped = resolve_leverage_used(max_leverage_available=20.0, leverage_cap=0.0)
        self.assertEqual(leverage, 20.0)
        self.assertFalse(capped)

    def test_invalid_max_leverage_raises(self):
        with self.assertRaises(ValueError):
            resolve_leverage_used(max_leverage_available=0.0, leverage_cap=None)


# ── 6. calculate_trade_risk (orchestrator async) ─────────────────────────

class TestCalculateTradeRiskSuccess(unittest.TestCase):

    def test_percent_mode_long_limit(self):
        patch_risk, patch_lev = _patch_settings(RiskMode.PERCENT, 1.0)
        client = FakeRestClient(
            total_equity=1000.0, free_margin=1000.0, max_leverage=20.0,
            ticker_price=100.0,  # dekat entry_price — lolos sanity-check deviasi
        )

        with patch_risk, patch_lev:
            result = run(calculate_trade_risk(
                direction=Direction.LONG,
                pair="BTC/USDT:USDT",
                entry_type=EntryType.LIMIT,
                entry_price=100.0,
                sl_price=95.0,
                rest_client=client,
            ))

        self.assertTrue(result.success)
        self.assertIsNone(result.failure_reason)
        self.assertEqual(result.risk_mode, RiskMode.PERCENT)
        self.assertAlmostEqual(result.risk_amount_usd, 10.0)   # 1% dari 1000
        self.assertAlmostEqual(result.risk_percent_used, 1.0)
        self.assertAlmostEqual(result.sl_distance, 5.0)
        self.assertAlmostEqual(result.position_size, 2.0)      # 10 / 5
        self.assertAlmostEqual(result.leverage_used, 20.0)     # max, tanpa cap
        self.assertFalse(result.leverage_capped_by_user)
        self.assertAlmostEqual(result.margin_needed, 10.0)     # (2*100)/20
        self.assertFalse(result.entry_price_estimated)

    def test_fixed_usd_mode(self):
        patch_risk, patch_lev = _patch_settings(RiskMode.FIXED_USD, 5.0)
        client = FakeRestClient(
            total_equity=50_000.0, free_margin=50_000.0, max_leverage=10.0,
            ticker_price=2.0,  # dekat entry_price — lolos sanity-check deviasi
        )

        with patch_risk, patch_lev:
            result = run(calculate_trade_risk(
                direction=Direction.LONG,
                pair="STG/USDT:USDT",
                entry_type=EntryType.LIMIT,
                entry_price=2.0,
                sl_price=1.8,
                rest_client=client,
            ))

        self.assertTrue(result.success)
        self.assertEqual(result.risk_mode, RiskMode.FIXED_USD)
        # Fixed $5 — TIDAK terpengaruh total_equity 50,000
        self.assertAlmostEqual(result.risk_amount_usd, 5.0)
        self.assertIsNone(result.risk_percent_used)
        self.assertAlmostEqual(result.sl_distance, 0.2)
        self.assertAlmostEqual(result.position_size, 25.0)     # 5 / 0.2
        self.assertAlmostEqual(result.margin_needed, 5.0)      # (25*2)/10

    def test_sl_distance_does_not_change_risk_amount(self):
        """
        Inti bagian 4.1/4.4: risk_amount KONSTAN apapun jarak SL-nya — hanya
        position_size & margin yang berubah.
        """
        patch_risk, patch_lev = _patch_settings(RiskMode.PERCENT, 1.0)

        client_near = FakeRestClient(
            total_equity=1000.0, free_margin=1000.0, max_leverage=20.0,
            ticker_price=100.0,  # dekat entry_price — lolos sanity-check deviasi
        )
        with patch_risk, patch_lev:
            near = run(calculate_trade_risk(
                direction=Direction.LONG,
                pair="A/USDT:USDT", entry_type=EntryType.LIMIT,
                entry_price=100.0, sl_price=99.0, rest_client=client_near,
            ))

        patch_risk2, patch_lev2 = _patch_settings(RiskMode.PERCENT, 1.0)
        client_far = FakeRestClient(
            total_equity=1000.0, free_margin=1000.0, max_leverage=20.0,
            ticker_price=100.0,  # dekat entry_price — lolos sanity-check deviasi
        )
        with patch_risk2, patch_lev2:
            far = run(calculate_trade_risk(
                direction=Direction.LONG,
                pair="A/USDT:USDT", entry_type=EntryType.LIMIT,
                entry_price=100.0, sl_price=50.0, rest_client=client_far,
            ))

        self.assertTrue(near.success and far.success)
        # risk_amount identik meski jarak SL sangat berbeda (1 vs 50)
        self.assertAlmostEqual(near.risk_amount_usd, far.risk_amount_usd)
        # position_size HARUS berbeda — SL dekat → position_size lebih besar
        self.assertGreater(near.position_size, far.position_size)

    def test_leverage_cap_applied_and_recalculates_margin(self):
        patch_risk, patch_lev = _patch_settings(RiskMode.PERCENT, 1.0, leverage_cap=5.0)
        client = FakeRestClient(
            total_equity=1000.0, free_margin=1000.0, max_leverage=50.0,
            ticker_price=100.0,  # dekat entry_price — lolos sanity-check deviasi
        )

        with patch_risk, patch_lev:
            result = run(calculate_trade_risk(
                direction=Direction.LONG,
                pair="ETH/USDT:USDT", entry_type=EntryType.LIMIT,
                entry_price=100.0, sl_price=95.0, rest_client=client,
            ))

        self.assertTrue(result.success)
        self.assertEqual(result.max_leverage_available, 50.0)
        self.assertEqual(result.leverage_used, 5.0)            # cap, bukan max
        self.assertTrue(result.leverage_capped_by_user)
        # margin_needed harus pakai leverage_used (5), bukan max_leverage (50)
        self.assertAlmostEqual(result.margin_needed, (result.position_size * 100.0) / 5.0)

    def test_market_order_without_explicit_price_uses_ticker(self):
        patch_risk, patch_lev = _patch_settings(RiskMode.PERCENT, 1.0)
        client = FakeRestClient(
            total_equity=1000.0, free_margin=1000.0, max_leverage=20.0, ticker_price=250.0,
        )

        with patch_risk, patch_lev:
            result = run(calculate_trade_risk(
                direction=Direction.LONG,
                pair="SOL/USDT:USDT", entry_type=EntryType.MARKET,
                entry_price=None, sl_price=240.0, rest_client=client,
            ))

        self.assertTrue(result.success)
        self.assertTrue(result.entry_price_estimated)
        self.assertAlmostEqual(result.entry_price_used, 250.0)
        self.assertEqual(client.fetch_ticker_price_calls, 1)
        self.assertAlmostEqual(result.sl_distance, 10.0)

    def test_market_order_with_explicit_price_still_sanity_checked(self):
        """
        Kalau sinyal sudah kasih harga market eksplisit, ticker TIDAK dipakai
        untuk estimasi harga entry (entry_price_estimated tetap False) — tapi
        TETAP dipanggil SATU KALI untuk sanity-check deviasi (bagian 2c):
        entry_price dari sinyal dibandingkan ke harga live untuk menangkap
        salah baca digit yang kebetulan masih di sisi SL yang "benar".
        (Sebelumnya test ini bernama *_skips_ticker dan mengharapkan 0 call —
        itu valid SEBELUM sanity-check 2c ditambahkan; sekarang 1 call adalah
        perilaku yang benar & disengaja.)
        """
        patch_risk, patch_lev = _patch_settings(RiskMode.PERCENT, 1.0)
        client = FakeRestClient(
            total_equity=1000.0, free_margin=1000.0, max_leverage=20.0,
            ticker_price=250.0,  # dekat entry_price — lolos sanity-check deviasi
        )

        with patch_risk, patch_lev:
            result = run(calculate_trade_risk(
                direction=Direction.LONG,
                pair="SOL/USDT:USDT", entry_type=EntryType.MARKET,
                entry_price=250.0, sl_price=240.0, rest_client=client,
            ))

        self.assertTrue(result.success)
        self.assertFalse(result.entry_price_estimated)
        self.assertEqual(client.fetch_ticker_price_calls, 1)


class TestCalculateTradeRiskFailure(unittest.TestCase):

    def test_insufficient_margin(self):
        """Margin dibutuhkan lebih besar dari free_margin → trade dibatalkan."""
        patch_risk, patch_lev = _patch_settings(RiskMode.PERCENT, 1.0)
        # equity besar (risk_amount besar) tapi free_margin sangat kecil & leverage rendah
        client = FakeRestClient(
            total_equity=100_000.0, free_margin=1.0, max_leverage=1.0,
            ticker_price=100.0,  # dekat entry_price — lolos sanity-check deviasi
        )

        with patch_risk, patch_lev:
            result = run(calculate_trade_risk(
                direction=Direction.LONG,
                pair="BTC/USDT:USDT", entry_type=EntryType.LIMIT,
                entry_price=100.0, sl_price=95.0, rest_client=client,
            ))

        self.assertFalse(result.success)
        self.assertEqual(result.failure_reason, "insufficient_margin")
        # Angka tetap terisi untuk keperluan notifikasi (bukan None semua)
        self.assertIsNotNone(result.risk_amount_usd)
        self.assertIsNotNone(result.margin_needed)
        self.assertGreater(result.margin_needed, result.free_margin)

    def test_entry_equals_sl_is_invalid(self):
        patch_risk, patch_lev = _patch_settings(RiskMode.PERCENT, 1.0)
        client = FakeRestClient()

        with patch_risk, patch_lev:
            result = run(calculate_trade_risk(
                direction=Direction.LONG,
                pair="BTC/USDT:USDT", entry_type=EntryType.LIMIT,
                entry_price=100.0, sl_price=100.0, rest_client=client,
            ))

        self.assertFalse(result.success)
        self.assertEqual(result.failure_reason, "invalid_sl_distance")
        # Tidak perlu sampai fetch balance/leverage — gagal lebih awal
        self.assertEqual(client.fetch_balance_calls, 0)
        self.assertEqual(client.get_max_leverage_calls, 0)

    def test_limit_order_missing_entry_price(self):
        """Defensif: harusnya sudah ditolak parser, tapi risk engine tetap aman kalau lolos."""
        patch_risk, patch_lev = _patch_settings(RiskMode.PERCENT, 1.0)
        client = FakeRestClient()

        with patch_risk, patch_lev:
            result = run(calculate_trade_risk(
                direction=Direction.LONG,
                pair="BTC/USDT:USDT", entry_type=EntryType.LIMIT,
                entry_price=None, sl_price=95.0, rest_client=client,
            ))

        self.assertFalse(result.success)
        self.assertEqual(result.failure_reason, "missing_entry_price")

    def test_balance_fetch_critical_error(self):
        patch_risk, patch_lev = _patch_settings(RiskMode.PERCENT, 1.0)
        client = FakeRestClient(
            fail_balance=CriticalError("auth gagal"),
            ticker_price=100.0,  # dekat entry_price — lolos sanity-check deviasi
        )

        with patch_risk, patch_lev:
            result = run(calculate_trade_risk(
                direction=Direction.LONG,
                pair="BTC/USDT:USDT", entry_type=EntryType.LIMIT,
                entry_price=100.0, sl_price=95.0, rest_client=client,
            ))

        self.assertFalse(result.success)
        self.assertEqual(result.failure_reason, "exchange_error")

    def test_leverage_fetch_transient_error(self):
        patch_risk, patch_lev = _patch_settings(RiskMode.PERCENT, 1.0)
        client = FakeRestClient(
            fail_leverage=TransientError("timeout"),
            ticker_price=100.0,  # dekat entry_price — lolos sanity-check deviasi
        )

        with patch_risk, patch_lev:
            result = run(calculate_trade_risk(
                direction=Direction.LONG,
                pair="BTC/USDT:USDT", entry_type=EntryType.LIMIT,
                entry_price=100.0, sl_price=95.0, rest_client=client,
            ))

        self.assertFalse(result.success)
        self.assertEqual(result.failure_reason, "exchange_error")

    def test_ticker_fetch_error_for_market_order(self):
        patch_risk, patch_lev = _patch_settings(RiskMode.PERCENT, 1.0)
        client = FakeRestClient(fail_ticker=CriticalError("symbol suspended"))

        with patch_risk, patch_lev:
            result = run(calculate_trade_risk(
                direction=Direction.LONG,
                pair="BTC/USDT:USDT", entry_type=EntryType.MARKET,
                entry_price=None, sl_price=95.0, rest_client=client,
            ))

        self.assertFalse(result.success)
        self.assertEqual(result.failure_reason, "exchange_error")


# ── 7. RiskCalculationResult.recompute_margin (dipakai Step 10 nanti) ───

class TestRecomputeMargin(unittest.TestCase):

    def test_recompute_changes_margin_not_risk(self):
        patch_risk, patch_lev = _patch_settings(RiskMode.PERCENT, 1.0)
        client = FakeRestClient(
            total_equity=1000.0, free_margin=1000.0, max_leverage=20.0,
            ticker_price=100.0,  # dekat entry_price — lolos sanity-check deviasi
        )

        with patch_risk, patch_lev:
            result = run(calculate_trade_risk(
                direction=Direction.LONG,
                pair="BTC/USDT:USDT", entry_type=EntryType.LIMIT,
                entry_price=100.0, sl_price=95.0, rest_client=client,
            ))

        original_risk_amount = result.risk_amount_usd
        original_position_size = result.position_size
        original_margin = result.margin_needed

        # Simulasikan leverage_engine (Step 10) menurunkan leverage demi safety
        result.recompute_margin(leverage_used=4.0)

        self.assertEqual(result.leverage_used, 4.0)
        self.assertNotAlmostEqual(result.margin_needed, original_margin)
        # risk_amount & position_size TIDAK BOLEH berubah karena leverage
        self.assertAlmostEqual(result.risk_amount_usd, original_risk_amount)
        self.assertAlmostEqual(result.position_size, original_position_size)
        self.assertAlmostEqual(result.margin_needed, (original_position_size * 100.0) / 4.0)

    def test_recompute_without_prior_calculation_raises(self):
        empty_result = RiskCalculationResult(success=False)
        with self.assertRaises(ValueError):
            empty_result.recompute_margin(leverage_used=10.0)


# ── 8. format_risk_notification ──────────────────────────────────────────

class TestFormatRiskNotification(unittest.TestCase):

    def test_success_notification_distinguishes_amounts(self):
        patch_risk, patch_lev = _patch_settings(RiskMode.PERCENT, 1.0)
        client = FakeRestClient(
            total_equity=1000.0, free_margin=1000.0, max_leverage=20.0,
            ticker_price=100.0,  # dekat entry_price — lolos sanity-check deviasi
        )

        with patch_risk, patch_lev:
            result = run(calculate_trade_risk(
                direction=Direction.LONG,
                pair="BTC/USDT:USDT", entry_type=EntryType.LIMIT,
                entry_price=100.0, sl_price=95.0, rest_client=client,
            ))

        text = format_risk_notification(result)
        self.assertIn("Max loss jika SL hit", text)
        self.assertIn("Margin yang akan dikunci", text)
        self.assertIn("10.00 USDT", text)   # risk_amount
        self.assertIn("BUKAN angka kerugian", text)  # warning pembeda

    def test_failure_notification_shows_reason(self):
        result = RiskCalculationResult(
            success=False,
            failure_reason="insufficient_margin",
            risk_amount_usd=10.0,
            margin_needed=500.0,
            free_margin=50.0,
        )
        text = format_risk_notification(result)
        self.assertIn("DIBATALKAN", text)
        self.assertIn("Margin", text)
        self.assertIn("500.00", text)
        self.assertIn("50.00", text)

    def test_leverage_capped_note_included(self):
        patch_risk, patch_lev = _patch_settings(RiskMode.PERCENT, 1.0, leverage_cap=5.0)
        client = FakeRestClient(
            total_equity=1000.0, free_margin=1000.0, max_leverage=50.0,
            ticker_price=100.0,  # dekat entry_price — lolos sanity-check deviasi
        )

        with patch_risk, patch_lev:
            result = run(calculate_trade_risk(
                direction=Direction.LONG,
                pair="ETH/USDT:USDT", entry_type=EntryType.LIMIT,
                entry_price=100.0, sl_price=95.0, rest_client=client,
            ))

        text = format_risk_notification(result)
        self.assertIn("dibatasi manual", text)

    def test_estimated_entry_note_included(self):
        patch_risk, patch_lev = _patch_settings(RiskMode.PERCENT, 1.0)
        client = FakeRestClient(
            total_equity=1000.0, free_margin=1000.0, max_leverage=20.0, ticker_price=250.0,
        )

        with patch_risk, patch_lev:
            result = run(calculate_trade_risk(
                direction=Direction.LONG,
                pair="SOL/USDT:USDT", entry_type=EntryType.MARKET,
                entry_price=None, sl_price=240.0, rest_client=client,
            ))

        text = format_risk_notification(result)
        self.assertIn("estimasi", text)


# ─────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    unittest.main(verbosity=2)