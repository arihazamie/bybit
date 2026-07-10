"""
tests/test_rest_client.py
==========================
Unit test untuk Step 7 — Bitget REST client dasar.

Semua test menggunakan mock/fake — TIDAK ada koneksi nyata ke Bitget.
Ini sesuai prinsip desain: unit test harus bisa jalan offline & cepat.

Cakupan test:
  1. Klasifikasi error (transient vs critical) — semua kategori ccxt
  2. _safe_float helper — edge case value kosong / invalid
  3. _parse_balance — 3 jalur parsing (raw V2 list, raw V2 dict, ccxt standard)
  4. BalanceInfo dataclass — field dan __str__
  5. MarketInfo dataclass — field
  6. _parse_markets — filter USDT-M + ekstraksi max_leverage
  7. @with_retry decorator — transient → retry, critical → langsung raise
  8. wrap_exchange_error — wrapping ke tipe yang tepat
  9. Singleton get_rest_client — idempotent
  10. find_symbol_by_base — pencarian berdasarkan base currency

Jalankan:
    python -m unittest tests.test_rest_client -v
"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import ccxt

# Import yang ditest
from exchange.bitget.retry import (
    CriticalError,
    TransientError,
    classify_exception,
    with_retry,
    wrap_exchange_error,
)
from exchange.bitget.rest_client import (
    BalanceInfo,
    BitgetRestClient,
    MarketInfo,
    _safe_float,
    get_rest_client,
    reset_rest_client,
)


# ── Helper ───────────────────────────────────────────────────────────────────

def run(coro):
    """Jalankan coroutine di event loop baru untuk test sinkron."""
    return asyncio.run(coro)


# ── 1. Klasifikasi error ─────────────────────────────────────────────────────

class TestClassifyException(unittest.TestCase):
    """Semua kategori ccxt harus terklasifikasi dengan benar."""

    # Transient — boleh retry
    def test_network_error_is_transient(self):
        self.assertEqual(classify_exception(ccxt.NetworkError("timeout")), "transient")

    def test_request_timeout_is_transient(self):
        self.assertEqual(classify_exception(ccxt.RequestTimeout("timed out")), "transient")

    def test_ddos_protection_is_transient(self):
        self.assertEqual(classify_exception(ccxt.DDoSProtection("rate limit")), "transient")

    def test_exchange_not_available_is_transient(self):
        self.assertEqual(classify_exception(ccxt.ExchangeNotAvailable("maintenance")), "transient")

    def test_invalid_nonce_is_transient(self):
        self.assertEqual(classify_exception(ccxt.InvalidNonce("nonce")), "transient")

    def test_connection_error_is_transient(self):
        self.assertEqual(classify_exception(ConnectionError("conn refused")), "transient")

    def test_asyncio_timeout_is_transient(self):
        self.assertEqual(classify_exception(asyncio.TimeoutError()), "transient")

    # Critical — jangan retry, trip circuit breaker
    def test_auth_error_is_critical(self):
        self.assertEqual(classify_exception(ccxt.AuthenticationError("invalid key")), "critical")

    def test_permission_denied_is_critical(self):
        self.assertEqual(classify_exception(ccxt.PermissionDenied("no perm")), "critical")

    def test_insufficient_funds_is_critical(self):
        self.assertEqual(classify_exception(ccxt.InsufficientFunds("no balance")), "critical")

    def test_bad_symbol_is_critical(self):
        self.assertEqual(classify_exception(ccxt.BadSymbol("FAKECOIN")), "critical")

    def test_bad_request_is_critical(self):
        self.assertEqual(classify_exception(ccxt.BadRequest("bad param")), "critical")

    def test_invalid_order_is_critical(self):
        self.assertEqual(classify_exception(ccxt.InvalidOrder("qty too small")), "critical")

    def test_order_not_found_is_critical(self):
        self.assertEqual(classify_exception(ccxt.OrderNotFound("123")), "critical")

    # Edge: ExchangeError base class dengan keyword maintenance → transient
    def test_exchange_error_maintenance_keyword_is_transient(self):
        exc = ccxt.ExchangeError("exchange under maintenance")
        self.assertEqual(classify_exception(exc), "transient")

    def test_exchange_error_rate_limit_keyword_is_transient(self):
        exc = ccxt.ExchangeError("rate limit exceeded")
        self.assertEqual(classify_exception(exc), "transient")

    def test_exchange_error_unknown_is_critical(self):
        exc = ccxt.ExchangeError("some unknown error")
        self.assertEqual(classify_exception(exc), "critical")

    # Unknown exception → critical (fail safe)
    def test_unknown_exception_is_critical(self):
        self.assertEqual(classify_exception(ValueError("random")), "critical")
        self.assertEqual(classify_exception(RuntimeError("bug")), "critical")


# ── 2. _safe_float helper ─────────────────────────────────────────────────────

class TestSafeFloat(unittest.TestCase):

    def test_normal_float_string(self):
        self.assertAlmostEqual(_safe_float("1234.56"), 1234.56)

    def test_integer_string(self):
        self.assertAlmostEqual(_safe_float("100"), 100.0)

    def test_actual_float(self):
        self.assertAlmostEqual(_safe_float(99.9), 99.9)

    def test_none_returns_default(self):
        self.assertEqual(_safe_float(None), 0.0)
        self.assertEqual(_safe_float(None, default=5.0), 5.0)

    def test_empty_string_returns_default(self):
        self.assertEqual(_safe_float(""), 0.0)

    def test_invalid_string_returns_default(self):
        self.assertEqual(_safe_float("abc"), 0.0)
        self.assertEqual(_safe_float("N/A"), 0.0)

    def test_zero_string(self):
        self.assertEqual(_safe_float("0"), 0.0)

    def test_negative_value(self):
        self.assertAlmostEqual(_safe_float("-50.5"), -50.5)


# ── 3. _parse_balance ─────────────────────────────────────────────────────────

class TestParseBalance(unittest.TestCase):
    """Test _parse_balance dengan berbagai format response Bitget."""

    def setUp(self):
        # BitgetRestClient tanpa env validation untuk test
        with patch("exchange.bitget.rest_client.settings") as mock_settings:
            mock_settings.BITGET_API_KEY = "test_key"
            mock_settings.BITGET_API_SECRET = "test_secret"
            mock_settings.BITGET_PASSPHRASE = "test_pass"
            mock_settings.BITGET_USE_SANDBOX = True
            self.client = BitgetRestClient(
                api_key="test_key",
                api_secret="test_secret",
                passphrase="test_pass",
                sandbox=True,
            )

    def _parse(self, raw: dict) -> BalanceInfo:
        return self.client._parse_balance(raw)

    def test_bitget_v2_list_format(self):
        """Format Bitget V2: info.data adalah list."""
        raw = {
            "USDT": {"total": 900.0, "free": 700.0},
            "info": {
                "code": "00000",
                "data": [{
                    "usdtEquity": "1050.50",
                    "available": "820.00",
                    "accountEquity": "1000.00",
                    "unrealizedPL": "50.50",
                }],
                "msg": "success",
            },
        }
        result = self._parse(raw)
        self.assertAlmostEqual(result.total_equity, 1050.50)
        self.assertAlmostEqual(result.free_margin, 820.00)
        self.assertAlmostEqual(result.wallet_balance, 1000.00)
        self.assertAlmostEqual(result.unrealized_pnl, 50.50)

    def test_bitget_v2_dict_format(self):
        """Format alternatif: info.data adalah dict langsung."""
        raw = {
            "USDT": {"total": 500.0, "free": 400.0},
            "info": {
                "data": {
                    "usdtEquity": "510.00",
                    "available": "410.00",
                    "accountEquity": "500.00",
                    "unrealizedPL": "10.00",
                }
            },
        }
        result = self._parse(raw)
        self.assertAlmostEqual(result.total_equity, 510.00)
        self.assertAlmostEqual(result.free_margin, 410.00)
        self.assertAlmostEqual(result.unrealized_pnl, 10.00)

    def test_fallback_ccxt_standard(self):
        """Jika raw info kosong, fallback ke ccxt standard balance."""
        raw = {
            "USDT": {"total": 750.0, "free": 600.0},
            "info": {},   # raw info tidak ada
        }
        result = self._parse(raw)
        self.assertAlmostEqual(result.total_equity, 750.0)
        self.assertAlmostEqual(result.free_margin, 600.0)
        self.assertAlmostEqual(result.unrealized_pnl, 0.0)

    def test_empty_raw_returns_zeros(self):
        """Akun baru / balance kosong — tidak crash, return semua 0."""
        result = self._parse({})
        self.assertEqual(result.total_equity, 0.0)
        self.assertEqual(result.free_margin, 0.0)
        self.assertEqual(result.unrealized_pnl, 0.0)

    def test_raw_equity_zero_fallback_to_ccxt(self):
        """Jika equity di raw adalah 0 tapi ccxt ada nilai, pakai ccxt."""
        raw = {
            "USDT": {"total": 300.0, "free": 200.0},
            "info": {
                "data": [{
                    "usdtEquity": "0",       # raw kosong
                    "available": "0",
                    "accountEquity": "0",
                    "unrealizedPL": "0",
                }]
            },
        }
        result = self._parse(raw)
        # equity 0 dari raw → fallback ke ccxt total 300
        self.assertAlmostEqual(result.total_equity, 300.0)

    def test_partial_raw_fields_with_ccxt_fallback(self):
        """Sebagian field ada di raw, yang kosong fallback ke ccxt."""
        raw = {
            "USDT": {"total": 500.0, "free": 400.0},
            "info": {
                "data": [{
                    "usdtEquity": "520.0",
                    # 'available' tidak ada → harus fallback ke ccxt free
                    "accountEquity": "500.0",
                    "unrealizedPL": "20.0",
                }]
            },
        }
        result = self._parse(raw)
        self.assertAlmostEqual(result.total_equity, 520.0)
        self.assertAlmostEqual(result.free_margin, 400.0)   # fallback ke ccxt free

    def test_no_naming_conflict(self):
        """Memastikan bug 'info' overwrite tidak terjadi lagi."""
        # Jika ada naming conflict, result akan TypeError (BalanceInfo bukan dict)
        raw = {
            "USDT": {"total": 100.0, "free": 80.0},
            "info": {"data": [{"usdtEquity": "100", "available": "80",
                                "accountEquity": "100", "unrealizedPL": "0"}]},
        }
        result = self._parse(raw)
        # Harus return BalanceInfo, bukan dict
        self.assertIsInstance(result, BalanceInfo)
        self.assertIsInstance(result.total_equity, float)


# ── 4. BalanceInfo dataclass ─────────────────────────────────────────────────

class TestBalanceInfo(unittest.TestCase):

    def test_fields(self):
        b = BalanceInfo(
            total_equity=1000.0, free_margin=800.0,
            wallet_balance=950.0, unrealized_pnl=50.0,
        )
        self.assertEqual(b.total_equity, 1000.0)
        self.assertEqual(b.free_margin, 800.0)
        self.assertEqual(b.wallet_balance, 950.0)
        self.assertEqual(b.unrealized_pnl, 50.0)

    def test_str_contains_key_values(self):
        b = BalanceInfo(total_equity=1234.56, free_margin=500.0,
                        wallet_balance=1200.0, unrealized_pnl=34.56)
        s = str(b)
        self.assertIn("1234", s)
        self.assertIn("500", s)

    def test_snapshot_at_is_set(self):
        import time
        before = time.time()
        b = BalanceInfo(total_equity=0, free_margin=0, wallet_balance=0, unrealized_pnl=0)
        after = time.time()
        self.assertGreaterEqual(b.snapshot_at, before)
        self.assertLessEqual(b.snapshot_at, after)


# ── 5. MarketInfo dataclass ───────────────────────────────────────────────────

class TestMarketInfo(unittest.TestCase):

    def test_fields(self):
        m = MarketInfo(
            symbol="BTC/USDT:USDT", base="BTC", quote="USDT",
            settle="USDT", max_leverage=125.0, min_leverage=1.0,
            contract_size=0.001, active=True,
        )
        self.assertEqual(m.symbol, "BTC/USDT:USDT")
        self.assertEqual(m.base, "BTC")
        self.assertEqual(m.max_leverage, 125.0)
        self.assertTrue(m.active)

    def test_raw_field_defaults_empty_dict(self):
        m = MarketInfo(
            symbol="ETH/USDT:USDT", base="ETH", quote="USDT",
            settle="USDT", max_leverage=50.0, min_leverage=1.0,
            contract_size=1.0, active=True,
        )
        self.assertIsInstance(m.raw, dict)
        self.assertEqual(len(m.raw), 0)


# ── 6. _parse_markets ─────────────────────────────────────────────────────────

class TestParseMarkets(unittest.TestCase):

    def setUp(self):
        self.client = BitgetRestClient(
            api_key="x", api_secret="x", passphrase="x", sandbox=True
        )

    def _make_market(self, sym, base, settle="USDT", swap=True, active=True, max_lev=50.0):
        return {
            "swap": swap,
            "settle": settle,
            "active": active,
            "base": base,
            "quote": "USDT",
            "contractSize": 1.0,
            "limits": {"leverage": {"max": max_lev, "min": 1.0}},
            "info": {},
        }

    def test_filters_non_swap(self):
        """Spot market harus difilter."""
        raw = {
            "BTC/USDT": self._make_market("BTC/USDT", "BTC", swap=False),
            "BTC/USDT:USDT": self._make_market("BTC/USDT:USDT", "BTC"),
        }
        result = self.client._parse_markets(raw)
        self.assertNotIn("BTC/USDT", result)
        self.assertIn("BTC/USDT:USDT", result)

    def test_filters_non_usdt_settle(self):
        """Kontrak settle BTC (coin-margined) harus difilter."""
        raw = {
            "BTC/USD:BTC": self._make_market("BTC/USD:BTC", "BTC", settle="BTC"),
            "ETH/USDT:USDT": self._make_market("ETH/USDT:USDT", "ETH"),
        }
        result = self.client._parse_markets(raw)
        self.assertNotIn("BTC/USD:BTC", result)
        self.assertIn("ETH/USDT:USDT", result)

    def test_filters_inactive(self):
        """Market inactive harus difilter."""
        raw = {
            "DEAD/USDT:USDT": self._make_market("DEAD/USDT:USDT", "DEAD", active=False),
            "LIVE/USDT:USDT": self._make_market("LIVE/USDT:USDT", "LIVE"),
        }
        result = self.client._parse_markets(raw)
        self.assertNotIn("DEAD/USDT:USDT", result)
        self.assertIn("LIVE/USDT:USDT", result)

    def test_extracts_max_leverage(self):
        raw = {"BTC/USDT:USDT": self._make_market("BTC/USDT:USDT", "BTC", max_lev=125.0)}
        result = self.client._parse_markets(raw)
        self.assertAlmostEqual(result["BTC/USDT:USDT"].max_leverage, 125.0)

    def test_leverage_fallback_to_raw_info(self):
        """Jika limits.leverage.max tidak ada, ambil dari raw info."""
        raw = {
            "ALT/USDT:USDT": {
                "swap": True, "settle": "USDT", "active": True,
                "base": "ALT", "quote": "USDT", "contractSize": 1.0,
                "limits": {"leverage": {}},   # max_lev tidak ada
                "info": {"maxLeverage": "20"},
            }
        }
        result = self.client._parse_markets(raw)
        self.assertAlmostEqual(result["ALT/USDT:USDT"].max_leverage, 20.0)

    def test_includes_all_categories(self):
        """Crypto, komoditas (XAU), dan saham semua harus masuk jika USDT-M."""
        raw = {
            "BTC/USDT:USDT":  self._make_market("BTC/USDT:USDT", "BTC"),
            "XAU/USDT:USDT":  self._make_market("XAU/USDT:USDT", "XAU"),
            "AAPL/USDT:USDT": self._make_market("AAPL/USDT:USDT", "AAPL"),
        }
        result = self.client._parse_markets(raw)
        self.assertIn("BTC/USDT:USDT", result)
        self.assertIn("XAU/USDT:USDT", result)
        self.assertIn("AAPL/USDT:USDT", result)


# ── 7. @with_retry decorator ──────────────────────────────────────────────────

class TestWithRetry(unittest.TestCase):

    def test_success_first_attempt(self):
        """Jika berhasil di percobaan pertama, tidak ada retry."""
        call_count = 0

        @with_retry(backoff=(0.01, 0.01))
        async def succeeds():
            nonlocal call_count
            call_count += 1
            return "ok"

        result = run(succeeds())
        self.assertEqual(result, "ok")
        self.assertEqual(call_count, 1)

    def test_transient_retries_then_succeeds(self):
        """Transient error di percobaan 1-2, berhasil di ke-3."""
        call_count = 0

        @with_retry(backoff=(0.01, 0.01, 0.01))
        async def fails_twice():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ccxt.NetworkError("timeout")
            return "recovered"

        result = run(fails_twice())
        self.assertEqual(result, "recovered")
        self.assertEqual(call_count, 3)

    def test_critical_no_retry(self):
        """Critical error langsung raise — tidak ada retry sama sekali."""
        call_count = 0

        @with_retry(backoff=(0.01, 0.01))
        async def critical_fail():
            nonlocal call_count
            call_count += 1
            raise ccxt.AuthenticationError("invalid key")

        with self.assertRaises(CriticalError):
            run(critical_fail())
        self.assertEqual(call_count, 1)   # dipanggil hanya sekali

    def test_all_retries_exhausted_raises_critical(self):
        """Semua percobaan transient gagal → akhirnya raise CriticalError."""
        @with_retry(backoff=(0.01, 0.01))
        async def always_fails():
            raise ccxt.NetworkError("always down")

        with self.assertRaises(CriticalError):
            run(always_fails())

    def test_pre_wrapped_critical_reraises(self):
        """CriticalError yang sudah dibungkus tidak di-double-wrap."""
        @with_retry(backoff=(0.01,))
        async def raises_critical():
            raise CriticalError("already wrapped")

        with self.assertRaises(CriticalError) as ctx:
            run(raises_critical())
        self.assertIn("already wrapped", str(ctx.exception))

    def test_pre_wrapped_transient_retries(self):
        """TransientError yang sudah dibungkus tetap di-retry."""
        call_count = 0

        @with_retry(backoff=(0.01, 0.01))
        async def raises_transient():
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise TransientError("network blip")
            return "ok"

        result = run(raises_transient())
        self.assertEqual(result, "ok")
        self.assertEqual(call_count, 2)


# ── 8. wrap_exchange_error ────────────────────────────────────────────────────

class TestWrapExchangeError(unittest.TestCase):

    def test_network_error_wrapped_as_transient(self):
        exc = ccxt.NetworkError("timeout")
        result = wrap_exchange_error(exc, context="fetch_balance")
        self.assertIsInstance(result, TransientError)
        self.assertIn("fetch_balance", str(result))
        self.assertIs(result.original, exc)

    def test_auth_error_wrapped_as_critical(self):
        exc = ccxt.AuthenticationError("invalid key")
        result = wrap_exchange_error(exc, context="ping")
        self.assertIsInstance(result, CriticalError)
        self.assertIn("ping", str(result))
        self.assertIs(result.original, exc)

    def test_no_context_still_works(self):
        exc = ccxt.InsufficientFunds("no balance")
        result = wrap_exchange_error(exc)
        self.assertIsInstance(result, CriticalError)


# ── 9. Singleton get_rest_client ──────────────────────────────────────────────

class TestSingleton(unittest.TestCase):

    def test_get_rest_client_same_instance(self):
        c1 = get_rest_client()
        c2 = get_rest_client()
        self.assertIs(c1, c2)

    def test_reset_creates_new_instance(self):
        c1 = get_rest_client()
        run(reset_rest_client())
        c2 = get_rest_client()
        self.assertIsNot(c1, c2)

    def tearDown(self):
        run(reset_rest_client())


# ── 10. find_symbol_by_base (dengan mocked market cache) ─────────────────────

class TestFindSymbolByBase(unittest.TestCase):

    def setUp(self):
        self.client = BitgetRestClient(
            api_key="x", api_secret="x", passphrase="x", sandbox=True
        )
        # Inject market cache langsung — tidak perlu network
        BitgetRestClient._market_cache = {
            "BTC/USDT:USDT": MarketInfo(
                symbol="BTC/USDT:USDT", base="BTC", quote="USDT",
                settle="USDT", max_leverage=125.0, min_leverage=1.0,
                contract_size=0.001, active=True,
            ),
            "XAU/USDT:USDT": MarketInfo(
                symbol="XAU/USDT:USDT", base="XAU", quote="USDT",
                settle="USDT", max_leverage=20.0, min_leverage=1.0,
                contract_size=1.0, active=True,
            ),
            "STG/USDT:USDT": MarketInfo(
                symbol="STG/USDT:USDT", base="STG", quote="USDT",
                settle="USDT", max_leverage=25.0, min_leverage=1.0,
                contract_size=1.0, active=True,
            ),
        }
        import time
        BitgetRestClient._market_cache_loaded_at = time.time()

    def tearDown(self):
        BitgetRestClient._market_cache = {}
        BitgetRestClient._market_cache_loaded_at = 0.0

    def test_find_crypto_by_base(self):
        result = run(self.client.find_symbol_by_base("BTC"))
        self.assertEqual(result, "BTC/USDT:USDT")

    def test_find_commodity_by_base(self):
        """XAU (emas) harus ditemukan — sesuai spec 'bukan crypto-only'."""
        result = run(self.client.find_symbol_by_base("XAU"))
        self.assertEqual(result, "XAU/USDT:USDT")

    def test_find_with_dollar_prefix(self):
        """Parser sinyal kirim '$STG' — $ harus di-strip."""
        result = run(self.client.find_symbol_by_base("$STG"))
        self.assertEqual(result, "STG/USDT:USDT")

    def test_case_insensitive(self):
        result = run(self.client.find_symbol_by_base("btc"))
        self.assertEqual(result, "BTC/USDT:USDT")

    def test_unknown_symbol_returns_none(self):
        result = run(self.client.find_symbol_by_base("FAKECOIN"))
        self.assertIsNone(result)

    def test_symbol_exists_true(self):
        result = run(self.client.symbol_exists("BTC/USDT:USDT"))
        self.assertTrue(result)

    def test_symbol_exists_false(self):
        """
        symbol_exists harus return False untuk simbol tidak dikenal.
        Karena get_market_info melakukan force_reload jika simbol tidak ada di cache,
        kita mock fetch_all_markets supaya tidak ada network call di test offline.
        """
        async def mock_fetch(force_reload=False):
            return BitgetRestClient._market_cache

        with patch.object(self.client, 'fetch_all_markets', side_effect=mock_fetch):
            result = run(self.client.symbol_exists("FAKE/USDT:USDT"))
        self.assertFalse(result)


# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    unittest.main(verbosity=2)
