"""
tests/test_parser.py
=====================
Unit test untuk Step 4 — signal parser (ekstraksi field dasar).

Memakai fake market validator (bukan koneksi ccxt/Bitget asli) supaya test
bisa jalan offline & cepat. Validator asli (query market list Bitget
sungguhan) ada di exchange/bitget/market_data.py dan dipakai di production.

Jalankan:
    python -m unittest tests.test_parser -v
"""

from __future__ import annotations

import unittest
from typing import Optional

from bot.parser.signal_parser import parse_signal
from core.constants import Direction, EntryType, ParseStatus
from exchange.bitget.market_data import MarketMatch

# ── Fake market list — mensimulasikan hasil query market list Bitget ──────
# Mencakup crypto DAN komoditas (XAU) untuk membuktikan parser tidak
# berasumsi crypto-only (lihat catatan koreksi di bagian 3 prompt.md).
_FAKE_MARKETS = {
    "STG": MarketMatch(symbol="STG/USDT:USDT", base="STG", category="crypto"),
    "HYPE": MarketMatch(symbol="HYPE/USDT:USDT", base="HYPE", category="crypto"),
    "BTC": MarketMatch(symbol="BTC/USDT:USDT", base="BTC", category="crypto"),
    "MORPHO": MarketMatch(symbol="MORPHO/USDT:USDT", base="MORPHO", category="crypto"),
    "XAU": MarketMatch(symbol="XAU/USDT:USDT", base="XAU", category="commodity"),
}


async def _fake_validator(pair_raw: str) -> Optional[MarketMatch]:
    return _FAKE_MARKETS.get(pair_raw.strip().upper().lstrip("$"))


class TestSignalParserFieldExtraction(unittest.IsolatedAsyncioTestCase):
    """5+ contoh sinyal real (format sesuai bagian 3 prompt.md)."""

    # ── Contoh 1: LONG, limit, crypto ─────────────────────────────────────
    async def test_long_limit_stg(self):
        text = (
            "🚀 SWING SETUP - LONG-buy\n\n"
            "🔘 Pair : $STG\n\n"
            "🔘 Time frame : 4H\n\n"
            "🔘 Entry limit 0.4520\n\n"
            "🔘 Target : di chart\n\n"
            "🔘 Stop loss : 0.4300\n\n"
            "🔖 ENTRY REASON : Breakout dari resistance daily, volume naik signifikan\n\n"
            "🔫 Risk Adjustment :\n"
            "*Max Loss / Risk Per Trade 1% of Total Trading Balance*"
        )
        r = await parse_signal(text, market_validator=_fake_validator)

        self.assertEqual(r.direction, Direction.LONG)
        self.assertEqual(r.pair_raw, "STG")
        self.assertEqual(r.pair_normalized, "STG/USDT:USDT")
        self.assertTrue(r.symbol_valid)
        self.assertEqual(r.market_category, "crypto")
        self.assertEqual(r.timeframe, "4H")
        self.assertEqual(r.entry_type, EntryType.LIMIT)
        self.assertEqual(r.entry_price, 0.4520)
        self.assertEqual(r.stop_loss, 0.4300)
        self.assertEqual(r.suggested_risk_percent, 1.0)
        self.assertIn("Breakout", r.entry_reason)
        self.assertEqual(r.parse_status, ParseStatus.SUCCESS)
        self.assertEqual(r.missing_fields, [])

    # ── Contoh 2: SHORT, market (tanpa harga eksplisit) ───────────────────
    async def test_short_market_hype(self):
        text = (
            "🚀 SWING SETUP - Short/sell\n\n"
            "🔘 Pair : $HYPE\n\n"
            "🔘 Time frame : 1H\n\n"
            "🔘 Entry market\n\n"
            "🔘 Target : di chart\n\n"
            "🔘 Stop loss : 28.50\n\n"
            "🔖 ENTRY REASON : Rejection di area supply, divergence RSI\n\n"
            "🔫 Risk Adjustment :\n"
            "*Max Loss / Risk Per Trade 2% of Total Trading Balance*"
        )
        r = await parse_signal(text, market_validator=_fake_validator)

        self.assertEqual(r.direction, Direction.SHORT)
        self.assertEqual(r.pair_normalized, "HYPE/USDT:USDT")
        self.assertEqual(r.entry_type, EntryType.MARKET)
        self.assertIsNone(r.entry_price)
        self.assertEqual(r.stop_loss, 28.50)
        self.assertEqual(r.suggested_risk_percent, 2.0)
        self.assertEqual(r.parse_status, ParseStatus.SUCCESS)

    # ── Contoh 3: LONG, market dengan harga referensi eksplisit ───────────
    async def test_long_market_with_explicit_price_btc(self):
        text = (
            "🚀 SWING SETUP - LONG-buy\n\n"
            "🔘 Pair : $BTC\n\n"
            "🔘 Time frame : 15m\n\n"
            "🔘 Entry market 67250\n\n"
            "🔘 Target : di chart\n\n"
            "🔘 Stop loss : 66000\n\n"
            "🔖 ENTRY REASON : Continuation trend naik, retest support kuat\n\n"
            "🔫 Risk Adjustment :\n"
            "*Max Loss / Risk Per Trade 1.5% of Total Trading Balance*"
        )
        r = await parse_signal(text, market_validator=_fake_validator)

        self.assertEqual(r.entry_type, EntryType.MARKET)
        self.assertEqual(r.entry_price, 67250.0)
        self.assertEqual(r.stop_loss, 66000.0)
        self.assertEqual(r.suggested_risk_percent, 1.5)
        self.assertEqual(r.parse_status, ParseStatus.SUCCESS)

    # ── Contoh 4: pair komoditas (XAU) — bukan crypto ─────────────────────
    async def test_long_limit_commodity_xau(self):
        text = (
            "🚀 SWING SETUP - LONG-buy\n\n"
            "🔘 Pair : $XAU\n\n"
            "🔘 Time frame : 1D\n\n"
            "🔘 Entry limit 2310.5\n\n"
            "🔘 Target : di chart\n\n"
            "🔘 Stop loss : 2285\n\n"
            "🔖 ENTRY REASON : Demand zone kuat di area 2300-2310\n\n"
            "🔫 Risk Adjustment :\n"
            "*Max Loss / Risk Per Trade 1% of Total Trading Balance*"
        )
        r = await parse_signal(text, market_validator=_fake_validator)

        self.assertEqual(r.pair_normalized, "XAU/USDT:USDT")
        self.assertEqual(r.market_category, "commodity")
        self.assertTrue(r.symbol_valid)
        self.assertEqual(r.parse_status, ParseStatus.SUCCESS)

    # ── Contoh 5: SHORT, limit, MORPHO ─────────────────────────────────────
    async def test_short_limit_morpho(self):
        text = (
            "🚀 SWING SETUP - Short/sell\n\n"
            "🔘 Pair : $MORPHO\n\n"
            "🔘 Time frame : 4H\n\n"
            "🔘 Entry limit 1.850\n\n"
            "🔘 Target : di chart\n\n"
            "🔘 Stop loss : 1.950\n\n"
            "🔖 ENTRY REASON : Rejection trendline turun, momentum bearish\n\n"
            "🔫 Risk Adjustment :\n"
            "*Max Loss / Risk Per Trade 1% of Total Trading Balance*"
        )
        r = await parse_signal(text, market_validator=_fake_validator)

        self.assertEqual(r.direction, Direction.SHORT)
        self.assertEqual(r.pair_normalized, "MORPHO/USDT:USDT")
        self.assertEqual(r.entry_type, EntryType.LIMIT)
        self.assertEqual(r.entry_price, 1.850)
        self.assertEqual(r.stop_loss, 1.950)
        self.assertEqual(r.parse_status, ParseStatus.SUCCESS)

    # ── Edge case: pair tidak dikenal di market list ───────────────────────
    async def test_unknown_pair_not_validated(self):
        text = (
            "🚀 SWING SETUP - LONG-buy\n\n"
            "🔘 Pair : $NOTAREALCOIN\n\n"
            "🔘 Time frame : 4H\n\n"
            "🔘 Entry limit 10\n\n"
            "🔘 Target : di chart\n\n"
            "🔘 Stop loss : 9\n\n"
            "🔖 ENTRY REASON : test\n\n"
            "🔫 Risk Adjustment :\n"
            "*Max Loss / Risk Per Trade 1% of Total Trading Balance*"
        )
        r = await parse_signal(text, market_validator=_fake_validator)

        self.assertFalse(r.symbol_valid)
        self.assertIsNone(r.pair_normalized)
        self.assertEqual(r.parse_status, ParseStatus.INVALID)
        self.assertIn("pair_normalized", r.missing_fields)
        self.assertTrue(any("tidak ditemukan" in n for n in r.notes))

    # ── Edge case: limit order tanpa harga eksplisit → invalid ────────────
    async def test_limit_without_price_is_invalid(self):
        text = (
            "🚀 SWING SETUP - LONG-buy\n\n"
            "🔘 Pair : $STG\n\n"
            "🔘 Time frame : 4H\n\n"
            "🔘 Entry limit\n\n"
            "🔘 Target : di chart\n\n"
            "🔘 Stop loss : 0.40\n\n"
            "🔖 ENTRY REASON : test\n\n"
            "🔫 Risk Adjustment :\n"
            "*Max Loss / Risk Per Trade 1% of Total Trading Balance*"
        )
        r = await parse_signal(text, market_validator=_fake_validator)

        self.assertEqual(r.entry_type, EntryType.LIMIT)
        self.assertIsNone(r.entry_price)
        self.assertEqual(r.parse_status, ParseStatus.INVALID)
        self.assertTrue(any("entry_price" in m for m in r.missing_fields))

    # ── Edge case: tidak ada Stop Loss sama sekali → invalid ───────────────
    async def test_missing_stop_loss_is_invalid(self):
        text = (
            "🚀 SWING SETUP - LONG-buy\n\n"
            "🔘 Pair : $STG\n\n"
            "🔘 Time frame : 4H\n\n"
            "🔘 Entry limit 0.45\n\n"
            "🔘 Target : di chart\n\n"
            "🔖 ENTRY REASON : tidak ada SL di pesan ini\n\n"
            "🔫 Risk Adjustment :\n"
            "*Max Loss / Risk Per Trade 1% of Total Trading Balance*"
        )
        r = await parse_signal(text, market_validator=_fake_validator)

        self.assertIsNone(r.stop_loss)
        self.assertIn("stop_loss", r.missing_fields)
        self.assertEqual(r.parse_status, ParseStatus.INVALID)

    # ── Edge case: validator melempar exception → parser tetap tidak crash ─
    async def test_validator_exception_does_not_crash_parser(self):
        async def _broken_validator(pair_raw: str):
            raise ConnectionError("simulasi Bitget API timeout")

        text = (
            "🚀 SWING SETUP - LONG-buy\n\n"
            "🔘 Pair : $STG\n\n"
            "🔘 Time frame : 4H\n\n"
            "🔘 Entry limit 0.45\n\n"
            "🔘 Target : di chart\n\n"
            "🔘 Stop loss : 0.40\n\n"
            "🔖 ENTRY REASON : test\n\n"
            "🔫 Risk Adjustment :\n"
            "*Max Loss / Risk Per Trade 1% of Total Trading Balance*"
        )
        r = await parse_signal(text, market_validator=_broken_validator)

        self.assertFalse(r.symbol_valid)
        self.assertEqual(r.parse_status, ParseStatus.INVALID)


if __name__ == "__main__":
    unittest.main()
