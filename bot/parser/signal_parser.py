"""
bot/parser/signal_parser.py
============================
Step 4 — Signal parser: ekstraksi field dasar dari format sinyal grup.

Tugas modul ini:
- Regex extraction untuk: arah (long/short), pair, tipe entry (limit/market),
  harga entry, stop loss, timeframe, entry reason, dan risk% yang disarankan
  analyst.
- Normalisasi pair ke unified symbol Bitget via query market list (semua
  kategori kontrak — crypto, komoditas, saham — lihat
  exchange/bitget/market_data.py).

BUKAN tugas modul ini (akan dikerjakan di Step 5):
- Confidence scoring & threshold ambiguitas (95%)
- Deteksi pesan info-only ("hit entry", "Close NR", "running NR") vs sinyal
  entry baru
- Keputusan final apakah sinyal dieksekusi otomatis atau dikirim ke alert
  manual untuk konfirmasi

Step 4 hanya menyediakan *field extraction* yang akurat plus daftar
`missing_fields` untuk field wajib yang gagal diparsing. Step 5 memakai
`missing_fields` + raw text ini sebagai input untuk confidence scoring dan
logika ambiguitas penuh.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from core.constants import Direction, EntryType, ParseStatus
from core.logging_setup import get_logger
from exchange.bitget.market_data import MarketMatch, get_default_market_cache

logger = get_logger(__name__)

# Callable async: terima pair mentah, kembalikan MarketMatch atau None.
# Dependency-injected supaya parser bisa di-unit-test tanpa koneksi exchange
# asli — lihat tests/test_parser.py untuk fake validator.
MarketValidator = Callable[[str], Awaitable[Optional[MarketMatch]]]


@dataclass
class ParsedSignal:
    """Hasil parsing satu pesan sinyal (field dasar saja — lihat docstring modul)."""

    raw_text: str

    direction: Optional[str] = None                 # Direction.LONG / Direction.SHORT
    pair_raw: Optional[str] = None                    # simbol mentah dari sinyal, mis. "STG"
    pair_normalized: Optional[str] = None              # unified symbol Bitget, mis. "STG/USDT:USDT"
    market_category: Optional[str] = None              # "crypto" | "commodity" | "unknown"

    timeframe: Optional[str] = None
    entry_type: Optional[str] = None                  # EntryType.LIMIT / EntryType.MARKET
    entry_price: Optional[float] = None                 # None = market order tanpa harga eksplisit

    stop_loss: Optional[float] = None

    entry_reason: Optional[str] = None
    suggested_risk_percent: Optional[float] = None      # saran analyst — BUKAN setting user aktif

    symbol_valid: bool = False
    missing_fields: list = field(default_factory=list)
    notes: list = field(default_factory=list)

    # Status sementara Step 4 — hanya SUCCESS / INVALID.
    # Step 5 menggantikan/menambah dengan AMBIGUOUS, INFO_ONLY, dst. berbasis
    # confidence scoring penuh.
    parse_status: str = ParseStatus.INVALID


# ── Regex patterns (mengikuti template di bagian 3 prompt.md) ────────────

_RE_DIRECTION_LINE = re.compile(r"SWING\s+SETUP\s*-\s*([^\n\r]+)", re.IGNORECASE)
_RE_PAIR = re.compile(r"Pair\s*:\s*\$?\s*([A-Za-z0-9][A-Za-z0-9._\-]*)", re.IGNORECASE)
_RE_TIMEFRAME = re.compile(r"Time\s*frame\s*:\s*([^\n\r]+)", re.IGNORECASE)
_RE_ENTRY = re.compile(
    r"Entry\s+(limit|market)\b\s*([0-9]+(?:[.,][0-9]+)?)?",
    re.IGNORECASE,
)
_RE_STOP_LOSS = re.compile(r"Stop\s*loss\s*:\s*([0-9]+(?:[.,][0-9]+)?)", re.IGNORECASE)
_RE_ENTRY_REASON = re.compile(
    r"ENTRY\s+REASON\s*:\s*(.+?)(?=\n\s*(?:\U0001F52B|Risk\s+Adjustment)|\Z)",
    re.IGNORECASE | re.DOTALL,
)
_RE_SUGGESTED_RISK = re.compile(
    r"Max\s+Loss\s*/\s*Risk\s+Per\s+Trade\s*([0-9]+(?:[.,][0-9]+)?)\s*%",
    re.IGNORECASE,
)


def _to_float(raw: Optional[str]) -> Optional[float]:
    if raw is None:
        return None
    cleaned = raw.strip().replace(",", "")
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _extract_direction(text: str) -> Optional[str]:
    m = _RE_DIRECTION_LINE.search(text)
    if not m:
        return None
    segment = m.group(1)
    if re.search(r"\blong\b", segment, re.IGNORECASE):
        return Direction.LONG
    if re.search(r"\bshort\b", segment, re.IGNORECASE):
        return Direction.SHORT
    return None


def _extract_entry(text: str) -> tuple:
    m = _RE_ENTRY.search(text)
    if not m:
        return None, None
    entry_type_raw = m.group(1).lower()
    entry_type = EntryType.LIMIT if entry_type_raw == "limit" else EntryType.MARKET
    entry_price = _to_float(m.group(2))
    # Limit order WAJIB punya harga eksplisit — tanpa harga dianggap tidak valid.
    if entry_type == EntryType.LIMIT and entry_price is None:
        return entry_type, None
    return entry_type, entry_price


def _extract_raw_fields(text: str) -> dict:
    """Ekstraksi murni via regex — tidak ada I/O, mudah di-unit-test sendiri."""
    direction = _extract_direction(text)

    pair_match = _RE_PAIR.search(text)
    pair_raw = pair_match.group(1).strip() if pair_match else None

    tf_match = _RE_TIMEFRAME.search(text)
    timeframe = tf_match.group(1).strip() if tf_match else None

    entry_type, entry_price = _extract_entry(text)

    sl_match = _RE_STOP_LOSS.search(text)
    stop_loss = _to_float(sl_match.group(1)) if sl_match else None

    reason_match = _RE_ENTRY_REASON.search(text)
    entry_reason = reason_match.group(1).strip() if reason_match else None

    risk_match = _RE_SUGGESTED_RISK.search(text)
    suggested_risk_percent = _to_float(risk_match.group(1)) if risk_match else None

    return {
        "direction": direction,
        "pair_raw": pair_raw,
        "timeframe": timeframe,
        "entry_type": entry_type,
        "entry_price": entry_price,
        "stop_loss": stop_loss,
        "entry_reason": entry_reason,
        "suggested_risk_percent": suggested_risk_percent,
    }


async def _default_validator(pair_raw: str) -> Optional[MarketMatch]:
    """Validator default — query market list Bitget asli (semua kategori) via ccxt."""
    cache = get_default_market_cache()
    return await cache.find_symbol(pair_raw)


async def parse_signal(
    text: str,
    market_validator: Optional[MarketValidator] = None,
) -> ParsedSignal:
    """
    Parse satu pesan sinyal mentah menjadi ParsedSignal.

    Args:
        text: teks pesan mentah dari Telegram (field `text` dari raw_event
              yang dihasilkan TelegramListener di Step 3).
        market_validator: async callable untuk validasi & normalisasi pair ke
              unified symbol Bitget. Default-nya query market list asli via
              ccxt (exchange/bitget/market_data.py). Override dengan fake
              validator saat unit testing supaya tidak butuh koneksi network.

    Returns:
        ParsedSignal — selalu dikembalikan, tidak pernah raise untuk format
        sinyal yang aneh. `missing_fields` berisi field wajib yang gagal
        diekstrak; `parse_status` SUCCESS hanya jika semua field wajib
        lengkap DAN pair tervalidasi di market list Bitget. Step 5 yang
        memutuskan apa yang terjadi pada sinyal yang gagal (ambigu vs
        info-only vs benar-benar invalid).
    """
    validator = market_validator or _default_validator

    raw = _extract_raw_fields(text)
    result = ParsedSignal(
        raw_text=text,
        direction=raw["direction"],
        timeframe=raw["timeframe"],
        entry_type=raw["entry_type"],
        entry_price=raw["entry_price"],
        stop_loss=raw["stop_loss"],
        entry_reason=raw["entry_reason"],
        suggested_risk_percent=raw["suggested_risk_percent"],
    )
    result.pair_raw = raw["pair_raw"]

    # ── Validasi & normalisasi pair via market list Bitget ───────────────
    if result.pair_raw:
        try:
            match = await validator(result.pair_raw)
        except Exception as exc:  # noqa: BLE001 — query market list tidak boleh crash parser
            logger.warning(
                "[parser] Gagal query market list Bitget untuk pair '%s': %s",
                result.pair_raw, exc,
            )
            match = None

        if match is not None:
            result.pair_normalized = match.symbol
            result.market_category = match.category
            result.symbol_valid = True
        else:
            result.notes.append(
                f"Pair '{result.pair_raw}' tidak ditemukan di market list Bitget "
                f"(sudah dicek di seluruh kategori kontrak USDT-M: crypto, "
                f"komoditas, saham)."
            )

    # ── Cek field wajib (lihat bagian 3 prompt.md) ────────────────────────
    missing = []
    if result.direction is None:
        missing.append("direction")
    if result.pair_normalized is None:
        missing.append("pair_normalized")
    if result.entry_type is None:
        missing.append("entry_type")
    elif result.entry_type == EntryType.LIMIT and result.entry_price is None:
        missing.append("entry_price (wajib untuk limit order)")
    if result.stop_loss is None:
        missing.append("stop_loss")

    result.missing_fields = missing
    result.parse_status = ParseStatus.SUCCESS if not missing else ParseStatus.INVALID

    if missing:
        logger.info(
            "[parser] Sinyal gagal diparsing lengkap — missing_fields=%s | pair_raw=%s",
            missing, result.pair_raw,
        )
    else:
        logger.info(
            "[parser] Sinyal berhasil diparsing | %s %s | entry=%s %s | SL=%s",
            result.direction, result.pair_normalized,
            result.entry_type, result.entry_price, result.stop_loss,
        )

    return result
