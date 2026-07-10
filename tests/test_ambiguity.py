"""
tests/test_ambiguity.py
=========================
Unit test untuk Step 5 — confidence scoring, deteksi ambiguitas, dan
pesan non-entry (update/follow-up).

Memakai fake market validator yang sama seperti tests/test_parser.py supaya
offline & tidak butuh koneksi Bitget asli.

Jalankan:
    python -m unittest tests.test_ambiguity -v
"""

from __future__ import annotations

import unittest
from typing import Optional

from bot.parser.ambiguity import (
    MessageType,
    classify_message_type,
    evaluate_signal,
)
from core.constants import InfoEventType, ParseStatus
from exchange.bitget.market_data import MarketMatch

_FAKE_MARKETS = {
    "STG": MarketMatch(symbol="STG/USDT:USDT", base="STG", category="crypto"),
    "HYPE": MarketMatch(symbol="HYPE/USDT:USDT", base="HYPE", category="crypto"),
    "MORPHO": MarketMatch(symbol="MORPHO/USDT:USDT", base="MORPHO", category="crypto"),
    "XAU": MarketMatch(symbol="XAU/USDT:USDT", base="XAU", category="commodity"),
}


async def _fake_validator(pair_raw: str) -> Optional[MarketMatch]:
    return _FAKE_MARKETS.get(pair_raw.strip().upper().lstrip("$"))


_VALID_SIGNAL = (
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


class TestMessageClassification(unittest.TestCase):
    """Klasifikasi awal pesan — sebelum field extraction dijalankan."""

    def test_full_template_is_new_signal_candidate(self):
        self.assertEqual(
            classify_message_type(_VALID_SIGNAL),
            MessageType.NEW_SIGNAL_CANDIDATE,
        )

    def test_hit_entry_classified_correctly(self):
        self.assertEqual(
            classify_message_type("$STG hit entry"),
            MessageType.INFO_HIT_ENTRY,
        )

    def test_close_nr_classified_correctly(self):
        self.assertEqual(
            classify_message_type("$STG Close 1.8R"),
            MessageType.INFO_CLOSE,
        )

    def test_manual_close_nr_classified_correctly(self):
        self.assertEqual(
            classify_message_type("$MORPHO manual close 1R"),
            MessageType.INFO_CLOSE,
        )

    def test_running_nr_classified_correctly(self):
        self.assertEqual(
            classify_message_type("$HYPE running 1R"),
            MessageType.INFO_RUNNING,
        )

    def test_random_chat_is_unknown(self):
        self.assertEqual(
            classify_message_type("anyone else seeing this dip? crazy market today"),
            MessageType.UNKNOWN,
        )


class TestInfoOnlyMessages(unittest.IsolatedAsyncioTestCase):
    """Pesan update/follow-up — TIDAK boleh memicu eksekusi order baru."""

    async def test_hit_entry(self):
        r = await evaluate_signal("$STG hit entry", market_validator=_fake_validator)
        self.assertEqual(r.parse_status, ParseStatus.INFO_ONLY)
        self.assertEqual(r.message_type, MessageType.INFO_HIT_ENTRY)
        self.assertEqual(r.info.event_type, InfoEventType.HIT_ENTRY)
        self.assertEqual(r.info.pair_raw, "STG")
        self.assertIsNone(r.info.r_multiple)
        self.assertIsNone(r.parsed)  # tidak lewat field-extraction sama sekali

    async def test_close_nr(self):
        r = await evaluate_signal("$STG Close 1.8R", market_validator=_fake_validator)
        self.assertEqual(r.parse_status, ParseStatus.INFO_ONLY)
        self.assertEqual(r.info.event_type, InfoEventType.CLOSE)
        self.assertEqual(r.info.r_multiple, 1.8)

    async def test_manual_close_nr(self):
        r = await evaluate_signal("$MORPHO manual close 1R", market_validator=_fake_validator)
        self.assertEqual(r.parse_status, ParseStatus.INFO_ONLY)
        self.assertEqual(r.info.event_type, InfoEventType.MANUAL_CLOSE)
        self.assertEqual(r.info.pair_raw, "MORPHO")
        self.assertEqual(r.info.r_multiple, 1.0)

    async def test_running_nr(self):
        r = await evaluate_signal("$HYPE running 1R", market_validator=_fake_validator)
        self.assertEqual(r.parse_status, ParseStatus.INFO_ONLY)
        self.assertEqual(r.info.event_type, InfoEventType.RUNNING)
        self.assertEqual(r.info.r_multiple, 1.0)


class TestAmbiguousSignals(unittest.IsolatedAsyncioTestCase):
    """Sinyal yang HARUS masuk jalur ambigu — JANGAN eksekusi."""

    async def test_unknown_pair_is_ambiguous(self):
        text = _VALID_SIGNAL.replace("$STG", "$NOTAREALCOIN")
        r = await evaluate_signal(text, market_validator=_fake_validator)
        self.assertEqual(r.parse_status, ParseStatus.AMBIGUOUS)
        self.assertTrue(any("Pair tidak dikenali" in reason for reason in r.ambiguous_reasons))
        self.assertLess(r.confidence, 95)

    async def test_missing_stop_loss_is_ambiguous(self):
        text = _VALID_SIGNAL.replace("🔘 Stop loss : 0.4300\n\n", "")
        r = await evaluate_signal(text, market_validator=_fake_validator)
        self.assertEqual(r.parse_status, ParseStatus.AMBIGUOUS)
        self.assertTrue(any("Stop Loss" in reason for reason in r.ambiguous_reasons))

    async def test_unclear_entry_type_is_ambiguous(self):
        text = _VALID_SIGNAL.replace("🔘 Entry limit 0.4520", "🔘 Entry TBD")
        r = await evaluate_signal(text, market_validator=_fake_validator)
        self.assertEqual(r.parse_status, ParseStatus.AMBIGUOUS)
        self.assertTrue(any("Entry type" in reason for reason in r.ambiguous_reasons))

    async def test_random_chat_is_ambiguous(self):
        r = await evaluate_signal(
            "guys gimana kabar market hari ini, rame banget",
            market_validator=_fake_validator,
        )
        self.assertEqual(r.parse_status, ParseStatus.AMBIGUOUS)
        self.assertEqual(r.message_type, MessageType.UNKNOWN)
        self.assertTrue(len(r.ambiguous_reasons) >= 1)

    async def test_full_valid_signal_is_success(self):
        r = await evaluate_signal(_VALID_SIGNAL, market_validator=_fake_validator)
        self.assertEqual(r.parse_status, ParseStatus.SUCCESS)
        self.assertEqual(r.confidence, 100)
        self.assertEqual(r.parsed.pair_normalized, "STG/USDT:USDT")
        self.assertEqual(r.ambiguous_reasons, [])


class TestDeviatedFormat(unittest.IsolatedAsyncioTestCase):
    """Format menyimpang dari template baku — bot tetap fleksibel tapi
    confidence harus turun, dan kalau di bawah threshold tetap masuk alert."""

    async def test_non_standard_header_uses_fallback_direction_but_low_confidence(self):
        # Header tidak pakai "SWING SETUP - LONG-buy" baku, tapi field lain lengkap
        text = (
            "📈 LONG Signal Alert\n\n"
            "🔘 Pair : $STG\n\n"
            "🔘 Time frame : 4H\n\n"
            "🔘 Entry limit 0.4520\n\n"
            "🔘 Stop loss : 0.4300\n\n"
            "🔖 ENTRY REASON : breakout"
        )
        r = await evaluate_signal(text, market_validator=_fake_validator)

        # Direction tetap ketemu via fallback...
        self.assertEqual(r.parsed.direction, "long")
        # ...tapi confidence-nya turun karena bukan header baku + bullet < 5
        self.assertLess(r.confidence, 100)
        self.assertTrue(
            any("format" in reason.lower() or "menyimpang" in reason.lower()
                for reason in r.ambiguous_reasons)
            or r.parse_status == ParseStatus.AMBIGUOUS
        )

    async def test_completely_garbled_format_has_zero_direction_credit(self):
        text = (
            "🔘 Pair : $STG\n"
            "🔘 Entry limit 0.4520\n"
            "🔘 Stop loss : 0.4300\n"
        )
        r = await evaluate_signal(text, market_validator=_fake_validator)
        self.assertIsNone(r.parsed.direction)
        self.assertEqual(r.parse_status, ParseStatus.AMBIGUOUS)
        self.assertTrue(any("Arah" in reason for reason in r.ambiguous_reasons))


if __name__ == "__main__":
    unittest.main()
