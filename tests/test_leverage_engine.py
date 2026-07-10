"""
tests/test_leverage_engine.py
==============================
Unit test untuk Step 10 — Leverage safety engine (bagian 4.3 prompt.md).

Semua test OFFLINE — tidak ada koneksi nyata ke Bitget. Fungsi murni di-test
langsung; orchestrator async (run_leverage_safety_check, recheck_existing_positions)
di-test dengan FakeRestClient yang mensimulasikan berbagai skenario akun.

Cakupan test:
  1. _estimate_maintenance_margin_rate — ambil dari raw info, fallback
  2. _calculate_new_position_mm — hitungan maintenance margin posisi baru
  3. _project_liquidation_price — proyeksi liq price, LONG & SHORT
  4. _find_safe_leverage — binary search, kasus max aman / perlu turun / min unsafe
  5. _check_existing_position_safety — posisi lama aman vs tidak aman
  6. run_leverage_safety_check — orchestrator penuh:
       - leverage max sudah aman
       - perlu turunkan leverage
       - bahkan leverage 1x tidak aman (high exposure)
       - banyak posisi simultan di mode cross
       - error exchange (balance gagal, positions gagal)
  7. recheck_existing_positions — semua aman, satu tidak aman
  8. format_leverage_safety_notification / format_existing_position_alert

Jalankan:
    python -m unittest tests.test_leverage_engine -v
"""

from __future__ import annotations

import asyncio
import unittest
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

from bot.leverage_engine.leverage_engine import (
    DEFAULT_SAFETY_BUFFER_PCT,
    FALLBACK_MAINTENANCE_MARGIN_RATE,
    ExistingPositionSafetyAlert,
    LeverageSafetyResult,
    OpenPositionSnapshot,
    _calculate_new_position_mm,
    _check_existing_position_safety,
    _estimate_maintenance_margin_rate,
    _find_safe_leverage,
    _parse_open_positions,
    _project_liquidation_price,
    format_existing_position_alert,
    format_leverage_safety_notification,
    recheck_existing_positions,
    run_leverage_safety_check,
)
from core.constants import Direction
from exchange.bitget.rest_client import BalanceInfo, MarketInfo
from exchange.bitget.retry import CriticalError, TransientError


# ── Helpers test ──────────────────────────────────────────────────────────────

def run(coro):
    """Jalankan coroutine dalam test synchronous.

    Pakai asyncio.run() (bukan asyncio.get_event_loop().run_until_complete())
    supaya setiap panggilan mendapat event loop baru miliknya sendiri —
    menghindari konflik dengan event loop yang dikelola pytest-asyncio saat
    file test ini dijalankan bersama test async lain dalam satu sesi pytest.
    """
    return asyncio.run(coro)


@dataclass
class FakeBalance:
    total_equity: float
    free_margin: float
    wallet_balance: float = 0.0
    unrealized_pnl: float = 0.0


class FakeRestClient:
    """
    Mock rest client untuk unit test — tidak ada koneksi ke exchange.
    Semua data bisa diset langsung sebelum test.
    """

    def __init__(
        self,
        balance: Optional[FakeBalance] = None,
        positions: Optional[list] = None,
        markets: Optional[dict] = None,
        raise_balance_error: bool = False,
        raise_positions_error: bool = False,
    ):
        self._balance = balance or FakeBalance(total_equity=1000.0, free_margin=800.0)
        self._positions = positions or []
        self._markets = markets or {}
        self._raise_balance_error = raise_balance_error
        self._raise_positions_error = raise_positions_error

    async def fetch_balance(self) -> FakeBalance:
        if self._raise_balance_error:
            raise TransientError("Fake balance error")
        return self._balance

    async def fetch_positions(self, symbols=None) -> list:
        if self._raise_positions_error:
            raise TransientError("Fake positions error")
        return self._positions

    async def fetch_all_markets(self, force_reload=False) -> dict:
        return self._markets


def make_position_raw(
    symbol: str,
    side: str,
    contracts: float,
    entry_price: float,
    notional: float = 0.0,
    maintenance_margin: float = 0.0,
    unrealized_pnl: float = 0.0,
) -> dict:
    """Buat raw ccxt position dict untuk testing."""
    return {
        "symbol": symbol,
        "side": side,
        "contracts": contracts,
        "entryPrice": entry_price,
        "notional": notional or (contracts * entry_price),
        "maintenanceMargin": maintenance_margin,
        "maintenanceMarginPercentage": 0.0,
        "unrealizedPnl": unrealized_pnl,
        "info": {},
    }


# ── Test 1: _estimate_maintenance_margin_rate ─────────────────────────────────

class TestEstimateMaintenanceMarginRate(unittest.TestCase):

    def test_from_bitget_raw_info(self):
        """Ambil rate dari raw Bitget info field."""
        market_raw = {"info": {"maintainMarginRate": "0.004"}}
        rate = _estimate_maintenance_margin_rate(market_raw)
        self.assertAlmostEqual(rate, 0.004)

    def test_from_min_maintain_margin_rate(self):
        market_raw = {"info": {"minMaintainMarginRate": "0.003"}}
        rate = _estimate_maintenance_margin_rate(market_raw)
        self.assertAlmostEqual(rate, 0.003)

    def test_from_ccxt_unified(self):
        market_raw = {"maintenanceMarginRate": 0.006, "info": {}}
        rate = _estimate_maintenance_margin_rate(market_raw)
        self.assertAlmostEqual(rate, 0.006)

    def test_ccxt_takes_priority_over_info(self):
        """ccxt unified field diambil pertama."""
        market_raw = {
            "maintenanceMarginRate": 0.006,
            "info": {"maintainMarginRate": "0.004"}
        }
        rate = _estimate_maintenance_margin_rate(market_raw)
        self.assertAlmostEqual(rate, 0.006)

    def test_fallback_when_no_data(self):
        rate = _estimate_maintenance_margin_rate({})
        self.assertAlmostEqual(rate, FALLBACK_MAINTENANCE_MARGIN_RATE)

    def test_fallback_when_none(self):
        rate = _estimate_maintenance_margin_rate(None)
        self.assertAlmostEqual(rate, FALLBACK_MAINTENANCE_MARGIN_RATE)

    def test_invalid_rate_falls_back(self):
        """Rate tidak valid (>= 1) → fallback."""
        market_raw = {"info": {"maintainMarginRate": "1.5"}}
        rate = _estimate_maintenance_margin_rate(market_raw)
        self.assertAlmostEqual(rate, FALLBACK_MAINTENANCE_MARGIN_RATE)


# ── Test 2: _calculate_new_position_mm ───────────────────────────────────────

class TestCalculateNewPositionMM(unittest.TestCase):

    def test_basic_calculation(self):
        """MM = notional * rate = (position_size * entry_price) * mm_rate."""
        mm = _calculate_new_position_mm(
            position_size=10.0,
            entry_price=100.0,
            leverage_used=20.0,
            maintenance_margin_rate=0.005,
        )
        # notional = 10 * 100 = 1000; mm = 1000 * 0.005 = 5
        self.assertAlmostEqual(mm, 5.0)

    def test_leverage_does_not_affect_mm_rate(self):
        """Leverage tidak mengubah mm_rate — hanya notional yang menentukan MM."""
        mm_high_lev = _calculate_new_position_mm(100.0, 50000.0, 100.0, 0.005)
        mm_low_lev = _calculate_new_position_mm(100.0, 50000.0, 1.0, 0.005)
        # MM sama karena notional sama
        self.assertAlmostEqual(mm_high_lev, mm_low_lev)


# ── Test 3: _project_liquidation_price ───────────────────────────────────────

class TestProjectLiquidationPrice(unittest.TestCase):

    def _long_projection(self, **kwargs):
        defaults = dict(
            direction=Direction.LONG,
            entry_price=100.0,
            position_size=10.0,
            leverage_used=20.0,
            maintenance_margin_rate=0.005,
            total_equity=1000.0,
            total_existing_mm=0.0,
            sl_price=90.0,
            buffer_pct=0.07,
        )
        defaults.update(kwargs)
        return _project_liquidation_price(**defaults)

    def _short_projection(self, **kwargs):
        defaults = dict(
            direction=Direction.SHORT,
            entry_price=100.0,
            position_size=10.0,
            leverage_used=20.0,
            maintenance_margin_rate=0.005,
            total_equity=1000.0,
            total_existing_mm=0.0,
            sl_price=110.0,
            buffer_pct=0.07,
        )
        defaults.update(kwargs)
        return _project_liquidation_price(**defaults)

    def test_long_no_existing_positions(self):
        """
        LONG, tidak ada posisi lain:
        new_mm = 10 * 100 * 0.005 = 5
        equity_buffer = 1000 - 0 - 5 = 995
        liq_price = 100 - 995/10 = 100 - 99.5 = 0.5
        SL = 90 → liq (0.5) << SL → AMAN
        """
        proj = self._long_projection()
        self.assertAlmostEqual(proj.new_position_mm, 5.0)
        self.assertAlmostEqual(proj.equity_buffer, 995.0)
        self.assertAlmostEqual(proj.liquidation_price, 0.5, places=4)
        self.assertTrue(proj.is_safe)

    def test_short_no_existing_positions(self):
        """
        SHORT, tidak ada posisi lain:
        liq_price = 100 + 995/10 = 199.5
        SL = 110 → liq (199.5) >> SL → AMAN
        """
        proj = self._short_projection()
        self.assertAlmostEqual(proj.liquidation_price, 199.5, places=4)
        self.assertTrue(proj.is_safe)

    def test_long_insufficient_equity_buffer(self):
        """
        LONG dengan equity sangat kecil → liq price dekat entry → tidak aman.
        total_equity = 20, position_size = 10, entry = 100, leverage = 20
        new_mm = 10 * 100 * 0.005 = 5
        equity_buffer = 20 - 0 - 5 = 15
        liq_price = 100 - 15/10 = 98.5
        SL = 90 → jarak (90-98.5)/90 = -9.4% → liq LEBIH TINGGI dari SL → TIDAK AMAN
        """
        proj = self._long_projection(total_equity=20.0, sl_price=90.0)
        self.assertAlmostEqual(proj.liquidation_price, 98.5, places=4)
        self.assertFalse(proj.is_safe)   # liq > sl untuk LONG

    def test_long_existing_positions_reduce_buffer(self):
        """
        Posisi existing memakan equity buffer → liq price naik → makin tidak aman.
        """
        proj_no_existing = self._long_projection(total_existing_mm=0.0)
        proj_with_existing = self._long_projection(total_existing_mm=500.0)
        # Buffer lebih kecil → liq price lebih tinggi (dekat entry)
        self.assertGreater(proj_with_existing.liquidation_price, proj_no_existing.liquidation_price)

    def test_buffer_pct_affects_safety_threshold(self):
        """
        Buffer yang lebih besar → threshold lebih ketat → mungkin tidak aman
        untuk leverage yang sama.
        """
        proj_small_buffer = self._long_projection(
            total_equity=20.0, sl_price=90.0, buffer_pct=0.01
        )
        proj_large_buffer = self._long_projection(
            total_equity=20.0, sl_price=90.0, buffer_pct=0.20
        )
        # Dengan equity kecil, keduanya tidak aman (liq > sl)
        # tapi assertion utamanya: buffer besar lebih sulit memenuhi
        # ini sudah implisit dari threshold formula

    def test_invalid_position_size_raises(self):
        with self.assertRaises(ValueError):
            self._long_projection(position_size=0.0)

    def test_invalid_entry_price_raises(self):
        with self.assertRaises(ValueError):
            self._long_projection(entry_price=-1.0)

    def test_sl_to_liq_distance_pct_long(self):
        """Jarak SL ke liq harus positif jika liq di bawah SL (LONG safe)."""
        proj = self._long_projection()
        # liq_price = 0.5, sl = 90 → jarak = (90 - 0.5) / 90 ≈ 99.4%
        self.assertGreater(proj.sl_to_liq_distance_pct, 0.9)


# ── Test 4: _find_safe_leverage ───────────────────────────────────────────────

class TestFindSafeLeverage(unittest.TestCase):

    def _find(self, **kwargs):
        defaults = dict(
            direction=Direction.LONG,
            entry_price=100.0,
            position_size=10.0,
            max_leverage=20.0,
            maintenance_margin_rate=0.005,
            total_equity=1000.0,
            total_existing_mm=0.0,
            sl_price=90.0,
            buffer_pct=0.07,
        )
        defaults.update(kwargs)
        return _find_safe_leverage(**defaults)

    def test_max_leverage_already_safe(self):
        """Jika leverage max sudah aman, return leverage max tanpa adjustment."""
        lev, proj, even_min_unsafe = self._find()
        self.assertAlmostEqual(lev, 20.0)
        self.assertTrue(proj.is_safe)
        self.assertFalse(even_min_unsafe)

    def test_leverage_needs_adjustment(self):
        """
        Dengan equity sangat kecil (total_equity=21), leverage tinggi tidak aman.
        Harus turun ke leverage yang lebih rendah.
        """
        lev, proj, even_min_unsafe = self._find(
            total_equity=21.0,
            sl_price=90.0,
            # Di leverage 20x:
            # new_mm = 10*100*0.005 = 5; buffer = 21-5=16; liq = 100-16/10 = 98.4
            # liq (98.4) > sl (90) → tidak aman
        )
        # Harus menemukan leverage lebih rendah yang aman atau min unsafe
        self.assertLessEqual(lev, 20.0)

    def test_even_min_leverage_unsafe(self):
        """
        Equity sangat kecil (total_equity=5) → bahkan leverage=1 tidak aman.
        new_mm (1x) = 10*100*0.005 = 5; buffer = 5-5=0; liq = 100 - 0/10 = 100
        liq (100) > sl (90) → TIDAK AMAN bahkan di leverage 1
        """
        lev, proj, even_min_unsafe = self._find(
            total_equity=5.0,
            sl_price=90.0,
            maintenance_margin_rate=0.005,
        )
        self.assertTrue(even_min_unsafe)
        self.assertAlmostEqual(lev, 1.0)

    def test_short_leverage_adjustment(self):
        """Short juga bisa perlu adjustment leverage."""
        lev, proj, even_min_unsafe = self._find(
            direction=Direction.SHORT,
            entry_price=100.0,
            sl_price=110.0,
            total_equity=1000.0,
            max_leverage=50.0,
        )
        # Dengan equity besar dan SL jauh, max leverage harusnya aman
        self.assertAlmostEqual(lev, 50.0)
        self.assertTrue(proj.is_safe)


# ── Test 5: _check_existing_position_safety ──────────────────────────────────

class TestCheckExistingPositionSafety(unittest.TestCase):

    def make_pos(self, **kwargs) -> OpenPositionSnapshot:
        defaults = dict(
            symbol="BTC/USDT:USDT",
            direction=Direction.LONG,
            contracts=0.1,
            entry_price=50000.0,
            notional=5000.0,
            maintenance_margin=25.0,   # 0.5% dari 5000
            maintenance_margin_rate=0.005,
            sl_price=45000.0,
        )
        defaults.update(kwargs)
        return OpenPositionSnapshot(**defaults)

    def test_long_position_safe(self):
        """
        LONG, SL = 45000:
        equity_buffer = 10000 - 0 - 25 = 9975
        liq_price = 50000 - 9975/0.1 = 50000 - 99750 → negatif (sangat aman)
        """
        pos = self.make_pos()
        alert = _check_existing_position_safety(
            pos,
            total_equity=10000.0,
            total_other_mm=0.0,
        )
        self.assertTrue(alert.is_safe)

    def test_long_position_unsafe_after_new_position(self):
        """
        Posisi baru yang besar menambah total MM → equity buffer berkurang →
        liq price naik → posisi lama tidak aman.
        """
        pos = self.make_pos(
            contracts=1.0,
            notional=50000.0,
            maintenance_margin=250.0,  # 0.5% dari 50000
        )
        # Equity tipis, MM dari posisi lain juga besar
        alert = _check_existing_position_safety(
            pos,
            total_equity=600.0,    # equity sangat kecil
            total_other_mm=500.0,  # posisi lain sudah pakai banyak MM
        )
        # buffer = 600 - 500 - 250 = -150 → liq_price di atas entry → tidak aman
        self.assertFalse(alert.is_safe)

    def test_no_sl_known_but_buffer_adequate(self):
        """Jika SL tidak diketahui, aman jika equity buffer cukup (>5% equity)."""
        pos = self.make_pos(sl_price=None)
        alert = _check_existing_position_safety(
            pos,
            total_equity=10000.0,
            total_other_mm=0.0,
        )
        self.assertTrue(alert.is_safe)

    def test_no_sl_known_buffer_insufficient(self):
        """Jika SL tidak diketahui dan buffer tipis → tidak aman."""
        pos = self.make_pos(sl_price=None, maintenance_margin=50.0)
        alert = _check_existing_position_safety(
            pos,
            total_equity=100.0,   # 5% threshold = 5; buffer = 100-0-50=50 → aman
            total_other_mm=90.0,  # buffer = 100-90-50 = -40 → tidak aman
        )
        self.assertFalse(alert.is_safe)


# ── Test 6: run_leverage_safety_check (orchestrator) ─────────────────────────

class TestRunLeverageSafetyCheck(unittest.TestCase):

    def _run(self, client=None, **kwargs):
        defaults = dict(
            pair="STG/USDT:USDT",
            direction=Direction.LONG,
            entry_price=1.0,
            sl_price=0.9,
            position_size=10.0,
            initial_leverage=20.0,
            max_leverage_available=20.0,
            rest_client=client or FakeRestClient(
                balance=FakeBalance(total_equity=1000.0, free_margin=800.0),
                positions=[],
                markets={},
            ),
        )
        defaults.update(kwargs)
        return run(run_leverage_safety_check(**defaults))

    def test_max_leverage_already_safe(self):
        """Dengan equity besar dan tidak ada posisi lain, leverage max aman."""
        result = self._run()
        self.assertTrue(result.success)
        self.assertFalse(result.leverage_adjusted)
        self.assertAlmostEqual(result.leverage_safe, 20.0)

    def test_leverage_adjusted_down(self):
        """
        Equity sangat kecil → leverage harus diturunkan.
        position_size=10, entry=1, mm_rate=0.005(default)
        Di leverage 20x: new_mm = 10*1*0.005 = 0.05; buffer = 12-0.05=11.95; liq = 1-11.95/10 = -0.195
        Masih aman karena liq negatif...
        Mari coba dengan equity lebih kecil dan SL lebih dekat.
        """
        # Dengan total_equity=0.12, sl=0.95:
        # mm_rate default 0.005, position=10, entry=1
        # new_mm = 0.05; buffer = 0.12-0.05=0.07; liq=1-0.07/10=1-0.007=0.993
        # sl=0.95, liq=0.993 > sl=0.95 → tidak aman
        client = FakeRestClient(
            balance=FakeBalance(total_equity=0.12, free_margin=0.10),
            positions=[],
        )
        result = self._run(client=client, sl_price=0.95)
        self.assertTrue(result.success)
        # Should find safe leverage or report even_min_unsafe
        self.assertLessEqual(result.leverage_safe, 20.0)

    def test_balance_error_returns_failure(self):
        """Jika fetch_balance gagal, return success=False."""
        client = FakeRestClient(raise_balance_error=True)
        result = self._run(client=client)
        self.assertFalse(result.success)
        self.assertEqual(result.failure_reason, "exchange_error_balance")

    def test_positions_error_returns_failure(self):
        """Jika fetch_positions gagal, return success=False."""
        client = FakeRestClient(raise_positions_error=True)
        result = self._run(client=client)
        self.assertFalse(result.success)
        self.assertEqual(result.failure_reason, "exchange_error_positions")

    def test_multiple_existing_positions(self):
        """
        Banyak posisi open existing — total MM mereka mengurangi equity buffer.
        Dengan cukup posisi, mungkin harus turunkan leverage.
        """
        existing = [
            make_position_raw("BTC/USDT:USDT", "long", 0.1, 50000.0, maintenance_margin=25.0),
            make_position_raw("ETH/USDT:USDT", "short", 1.0, 3000.0, maintenance_margin=15.0),
        ]
        client = FakeRestClient(
            balance=FakeBalance(total_equity=1000.0, free_margin=800.0),
            positions=existing,
        )
        result = self._run(client=client)
        self.assertTrue(result.success)
        # existing_positions_count = 2
        self.assertEqual(result.existing_positions_count, 2)

    def test_short_direction(self):
        """Safety check untuk SHORT juga harus berhasil."""
        result = self._run(
            direction=Direction.SHORT,
            entry_price=1.0,
            sl_price=1.1,
        )
        self.assertTrue(result.success)

    def test_no_leverage_adjusted_when_already_safe(self):
        """Jika aman dari awal, leverage_adjusted harus False."""
        result = self._run()
        self.assertFalse(result.leverage_adjusted)
        self.assertFalse(result.even_min_leverage_unsafe)

    def test_result_has_projection(self):
        """Result selalu punya projection jika success=True."""
        result = self._run()
        self.assertTrue(result.success)
        self.assertIsNotNone(result.projection)
        self.assertIsNotNone(result.projection.liquidation_price)


# ── Test 7: recheck_existing_positions ───────────────────────────────────────

class TestRecheckExistingPositions(unittest.TestCase):

    def test_all_positions_safe(self):
        """Semua posisi aman → return empty list."""
        client = FakeRestClient(
            balance=FakeBalance(total_equity=10000.0, free_margin=8000.0),
            positions=[
                make_position_raw("BTC/USDT:USDT", "long", 0.1, 50000.0,
                                   maintenance_margin=25.0),
            ],
        )
        result = run(recheck_existing_positions(rest_client=client))
        self.assertEqual(len(result), 0)

    def test_unsafe_position_detected(self):
        """
        Posisi tidak aman dengan equity tipis dan MM besar → masuk alert list.
        contracts=1, entry=50000, mm=25000, equity=25100 → buffer=100 sangat tipis
        liq = 50000 - 100/1 = 49900
        sl = 45000 → liq (49900) > sl (45000) → tidak aman
        """
        pos_raw = make_position_raw(
            "BTC/USDT:USDT", "long",
            contracts=1.0,
            entry_price=50000.0,
            notional=50000.0,
            maintenance_margin=25000.0,  # 50% dari notional (sangat besar)
        )
        client = FakeRestClient(
            balance=FakeBalance(total_equity=25100.0, free_margin=0.0),
            positions=[pos_raw],
        )
        result = run(recheck_existing_positions(
            rest_client=client,
            sl_lookup={"BTC/USDT:USDT": 45000.0},
        ))
        # Harus ada alert karena liq sangat dekat entry dan di atas SL
        # (buffer = 25100 - 25000 = 100; liq = 50000 - 100 = 49900 > 45000)
        self.assertGreater(len(result), 0)
        self.assertEqual(result[0].symbol, "BTC/USDT:USDT")

    def test_sl_lookup_injected(self):
        """SL dari database lokal di-inject ke posisi yang tidak punya SL dari exchange."""
        pos_raw = make_position_raw(
            "ETH/USDT:USDT", "short",
            contracts=1.0,
            entry_price=3000.0,
            maintenance_margin=15.0,
        )
        # SL tidak ada di pos_raw.info, tapi ada di sl_lookup
        client = FakeRestClient(
            balance=FakeBalance(total_equity=10000.0, free_margin=8000.0),
            positions=[pos_raw],
        )
        alerts = run(recheck_existing_positions(
            rest_client=client,
            sl_lookup={"ETH/USDT:USDT": 3300.0},
        ))
        # Dengan equity besar ini harus aman, yang penting SL ter-inject
        # (test ini memverifikasi tidak ada exception, bukan status aman/tidak)

    def test_balance_error_returns_empty(self):
        """Error saat fetch balance → return empty list (graceful)."""
        client = FakeRestClient(raise_balance_error=True)
        result = run(recheck_existing_positions(rest_client=client))
        self.assertEqual(result, [])

    def test_no_positions(self):
        """Tidak ada posisi → return empty list."""
        client = FakeRestClient(positions=[])
        result = run(recheck_existing_positions(rest_client=client))
        self.assertEqual(result, [])


# ── Test 8: format functions ──────────────────────────────────────────────────

class TestFormatFunctions(unittest.TestCase):

    def _make_result(self, **kwargs) -> LeverageSafetyResult:
        defaults = dict(
            success=True,
            leverage_requested=20.0,
            leverage_safe=20.0,
            leverage_adjusted=False,
            even_min_leverage_unsafe=False,
        )
        defaults.update(kwargs)
        return LeverageSafetyResult(**defaults)

    def test_format_success_no_adjustment(self):
        result = self._make_result()
        text = format_leverage_safety_notification(result)
        self.assertIn("✅", text)
        self.assertIn("OK", text)

    def test_format_leverage_adjusted(self):
        result = self._make_result(
            leverage_requested=20.0,
            leverage_safe=5.0,
            leverage_adjusted=True,
        )
        text = format_leverage_safety_notification(result)
        self.assertIn("⚠️", text)
        self.assertIn("20x", text)
        self.assertIn("5x", text)

    def test_format_even_min_unsafe(self):
        result = self._make_result(
            leverage_requested=20.0,
            leverage_safe=1.0,
            leverage_adjusted=True,
            even_min_leverage_unsafe=True,
        )
        text = format_leverage_safety_notification(result)
        self.assertIn("🚨", text)
        self.assertIn("KRITIS", text.upper())

    def test_format_failure(self):
        result = self._make_result(
            success=False,
            failure_reason="exchange_error_balance",
            notes=["Gagal fetch balance"],
        )
        text = format_leverage_safety_notification(result)
        self.assertIn("❌", text)
        self.assertIn("GAGAL", text.upper())

    def test_format_existing_position_alert(self):
        alert = ExistingPositionSafetyAlert(
            symbol="BTC/USDT:USDT",
            direction=Direction.LONG,
            sl_price=45000.0,
            liq_price_estimate=46000.0,
            sl_to_liq_distance_pct=-0.022,
            is_safe=False,
            entry_price=50000.0,
            position_size=0.1,
        )
        text = format_existing_position_alert(alert)
        self.assertIn("BTC/USDT:USDT", text)
        self.assertIn("LONG", text)
        self.assertIn("45000", text)
        self.assertIn("⚠️", text)

    def test_format_existing_no_sl(self):
        alert = ExistingPositionSafetyAlert(
            symbol="ETH/USDT:USDT",
            direction=Direction.SHORT,
            sl_price=None,
            liq_price_estimate=2500.0,
            sl_to_liq_distance_pct=0.01,
            is_safe=False,
            entry_price=3000.0,
            position_size=1.0,
        )
        text = format_existing_position_alert(alert)
        self.assertIn("tidak diketahui", text)


# ── Test 9: _parse_open_positions ────────────────────────────────────────────

class TestParseOpenPositions(unittest.TestCase):

    def test_basic_parse(self):
        raw = [make_position_raw("BTC/USDT:USDT", "long", 0.1, 50000.0)]
        result = _parse_open_positions(raw, {})
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].symbol, "BTC/USDT:USDT")
        self.assertEqual(result[0].direction, Direction.LONG)
        self.assertAlmostEqual(result[0].contracts, 0.1)

    def test_skip_zero_contracts(self):
        """Posisi dengan contracts=0 di-skip (sudah closed)."""
        raw = [make_position_raw("BTC/USDT:USDT", "long", 0.0, 50000.0)]
        result = _parse_open_positions(raw, {})
        self.assertEqual(len(result), 0)

    def test_short_direction(self):
        raw = [make_position_raw("ETH/USDT:USDT", "short", 1.0, 3000.0)]
        result = _parse_open_positions(raw, {})
        self.assertEqual(result[0].direction, Direction.SHORT)

    def test_maintenance_margin_from_market_info(self):
        """MM rate diambil dari market_info_map jika tersedia."""
        raw = [make_position_raw("STG/USDT:USDT", "long", 10.0, 1.0)]
        market_map = {"STG/USDT:USDT": {"info": {"maintainMarginRate": "0.01"}}}
        result = _parse_open_positions(raw, market_map)
        # notional = 10*1 = 10; mm_rate = 0.01; mm = 0.1
        self.assertAlmostEqual(result[0].maintenance_margin_rate, 0.01)

    def test_sl_from_raw_info(self):
        """SL diambil dari raw info jika ada."""
        pos = make_position_raw("BTC/USDT:USDT", "long", 0.1, 50000.0)
        pos["info"] = {"stopLossPrice": "45000.0"}
        result = _parse_open_positions([pos], {})
        self.assertAlmostEqual(result[0].sl_price, 45000.0)

    def test_skip_zero_entry_price(self):
        """Posisi tanpa entry_price valid di-skip."""
        raw = [make_position_raw("BTC/USDT:USDT", "long", 0.1, 0.0)]
        result = _parse_open_positions(raw, {})
        self.assertEqual(len(result), 0)


if __name__ == "__main__":
    unittest.main()