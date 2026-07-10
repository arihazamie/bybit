"""
bot/parser
==========
Signal parser:
- Step 4: ekstraksi field dasar dari pesan sinyal grup Telegram.
- Step 5: confidence scoring & deteksi ambiguitas/pesan non-entry.

Entry point yang dipakai pipeline (Step 19): `evaluate_signal()`.
"""

from bot.parser.signal_parser import ParsedSignal, parse_signal
from bot.parser.ambiguity import (
    InfoMessage,
    SignalEvaluation,
    classify_message_type,
    compute_confidence,
    evaluate_signal,
    parse_info_message,
)

__all__ = [
    "ParsedSignal",
    "parse_signal",
    "InfoMessage",
    "SignalEvaluation",
    "classify_message_type",
    "compute_confidence",
    "evaluate_signal",
    "parse_info_message",
]
