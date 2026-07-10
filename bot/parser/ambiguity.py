"""
bot/parser/ambiguity.py
=========================
Step 5 — Signal parser: ambiguitas & pesan non-entry.

Tugas modul ini (di atas hasil ekstraksi field Step 4):
1. **Klasifikasi pesan** sebelum field extraction: apakah ini kandidat
   sinyal entry baru, atau pesan update/follow-up (hit entry, Close NR,
   running NR) — lihat bagian 3 prompt.md "Pesan non-entry yang juga harus
   dikenali".
2. **Confidence scoring** (threshold default 95%, lihat bagian 9 prinsip #3
   & settings.PARSER_CONFIDENCE_THRESHOLD) — kalau parser tidak yakin
   terhadap field wajib → treat sebagai ambigu, JANGAN eksekusi.
3. **Handler format menyimpang** — analyst kadang menulis beda gaya dari
   template baku. Parser tetap mencoba fleksibel (mis. fallback deteksi
   direction di luar pola header baku), tapi kalau confidence akhirnya
   rendah → tetap masuk jalur ambigu/alert manual, BUKAN auto-eksekusi.

Entry point utama: `evaluate_signal(text)` — dipakai pipeline (Step 19) untuk
memutuskan jalur sinyal: SUCCESS (eksekusi) / AMBIGUOUS (alert manual) /
INFO_ONLY (cuma log/notifikasi) / INVALID.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from core.constants import InfoEventType, MessageType, ParseStatus
from core.logging_setup import get_logger
from bot.parser.signal_parser import (
    MarketValidator,
    ParsedSignal,
    parse_signal,
)

logger = get_logger(__name__)

# Threshold default kalau settings tidak diberikan eksplisit (lihat bagian 9 #3)
DEFAULT_CONFIDENCE_THRESHOLD = 95

# Bobot tiap komponen field wajib — total 100 kalau semua lengkap & baku.
_WEIGHT_DIRECTION = 20
_WEIGHT_DIRECTION_FALLBACK = 10  # kredit parsial kalau direction cuma ketemu via fallback
_WEIGHT_PAIR_VALID = 25
_WEIGHT_ENTRY_TYPE = 15
_WEIGHT_ENTRY_PRICE = 10
_WEIGHT_STOP_LOSS = 25
_WEIGHT_TEMPLATE_STRUCTURE = 5
# 20 + 25 + 15 + 10 + 25 + 5 = 100


# ── Regex: pesan non-entry (update/follow-up) ─────────────────────────────
# Semua pattern sengaja fleksibel ($ opsional, spasi bebas, case-insensitive)
# karena analyst menulis update jauh lebih singkat & tidak baku dibanding
# sinyal entry penuh.

_RE_HIT_ENTRY = re.compile(
    r"^\s*\$?([A-Za-z0-9]+)\s+hit\s+entry\b", re.IGNORECASE
)
_RE_MANUAL_CLOSE = re.compile(
    r"^\s*\$?([A-Za-z0-9]+)\s+manual\s+close\s+([0-9]+(?:\.[0-9]+)?)\s*R\b",
    re.IGNORECASE,
)
_RE_CLOSE = re.compile(
    r"^\s*\$?([A-Za-z0-9]+)\s+close\s+([0-9]+(?:\.[0-9]+)?)\s*R\b",
    re.IGNORECASE,
)
_RE_RUNNING = re.compile(
    r"^\s*\$?([A-Za-z0-9]+)\s+running\s+([0-9]+(?:\.[0-9]+)?)\s*R\b",
    re.IGNORECASE,
)

# Marker yang menandakan ini "kandidat" sinyal entry baru (longgar — tidak
# mewajibkan emoji persis, supaya format yang sedikit menyimpang tetap masuk
# jalur field-extraction penuh, bukan langsung dibuang sebagai UNKNOWN).
_RE_HEADER = re.compile(r"SWING\s+SETUP", re.IGNORECASE)
_RE_HAS_PAIR_KEYWORD = re.compile(r"\bPair\s*:", re.IGNORECASE)
_RE_HAS_ENTRY_KEYWORD = re.compile(r"\bEntry\b", re.IGNORECASE)
_RE_HAS_SL_KEYWORD = re.compile(r"Stop\s*loss\s*:", re.IGNORECASE)

# Fallback direction — dipakai HANYA kalau header "SWING SETUP - ..." baku
# tidak ketemu (format menyimpang), supaya bot tetap fleksibel tapi dengan
# kredit confidence yang lebih rendah (lihat _WEIGHT_DIRECTION_FALLBACK).
_RE_FALLBACK_LONG = re.compile(r"\b(long|buy)\b", re.IGNORECASE)
_RE_FALLBACK_SHORT = re.compile(r"\b(short|sell)\b", re.IGNORECASE)

# Template structure check — minimal berapa kali bullet "🔘" muncul supaya
# dianggap mengikuti format baku (template asli punya 5 bullet field).
_TEMPLATE_BULLET = "\U0001F518"  # 🔘
_TEMPLATE_MIN_BULLETS = 3


@dataclass
class InfoMessage:
    """Hasil parsing pesan non-entry (update/follow-up dari analyst)."""

    raw_text: str
    event_type: str           # InfoEventType.*
    pair_raw: str
    r_multiple: Optional[float] = None  # None untuk hit_entry (tidak ada angka R)


@dataclass
class SignalEvaluation:
    """Hasil akhir evaluasi satu pesan — dipakai pipeline untuk ambil keputusan."""

    raw_text: str
    message_type: str                      # MessageType.*
    parse_status: str                      # ParseStatus.* (keputusan FINAL)

    parsed: Optional[ParsedSignal] = None   # diisi kalau message_type == NEW_SIGNAL_CANDIDATE
    info: Optional[InfoMessage] = None      # diisi kalau message_type == INFO_*

    confidence: Optional[int] = None        # 0-100, hanya untuk NEW_SIGNAL_CANDIDATE
    confidence_breakdown: dict = field(default_factory=dict)
    ambiguous_reasons: list = field(default_factory=list)


# ── Klasifikasi pesan ──────────────────────────────────────────────────────

def classify_message_type(text: str) -> str:
    """
    Tentukan jenis pesan SEBELUM field extraction penuh dijalankan.

    Urutan cek (penting — info-only dicek lebih dulu karena formatnya jauh
    lebih spesifik & singkat dibanding sinyal entry penuh):
    1. Pesan update non-entry (hit entry / close NR / running NR)
    2. Kandidat sinyal entry baru (ada header baku ATAU kombinasi keyword
       Pair+Entry/SL — supaya format yang sedikit menyimpang tetap dicoba
       diparse, bukan langsung dibuang)
    3. UNKNOWN — tidak match pola apapun
    """
    stripped = text.strip()

    if _RE_MANUAL_CLOSE.search(stripped):
        return MessageType.INFO_CLOSE  # manual close dicek duluan (lebih spesifik dari close biasa)
    if _RE_CLOSE.search(stripped):
        return MessageType.INFO_CLOSE
    if _RE_RUNNING.search(stripped):
        return MessageType.INFO_RUNNING
    if _RE_HIT_ENTRY.search(stripped):
        return MessageType.INFO_HIT_ENTRY

    looks_like_signal = (
        _RE_HEADER.search(stripped) is not None
        or (
            _RE_HAS_PAIR_KEYWORD.search(stripped) is not None
            and (
                _RE_HAS_ENTRY_KEYWORD.search(stripped) is not None
                or _RE_HAS_SL_KEYWORD.search(stripped) is not None
            )
        )
    )
    if looks_like_signal:
        return MessageType.NEW_SIGNAL_CANDIDATE

    return MessageType.UNKNOWN


def parse_info_message(text: str, message_type: str) -> Optional[InfoMessage]:
    """Ekstrak pair + R-multiple dari pesan non-entry sesuai message_type."""
    stripped = text.strip()

    if message_type == MessageType.INFO_HIT_ENTRY:
        m = _RE_HIT_ENTRY.search(stripped)
        if not m:
            return None
        return InfoMessage(
            raw_text=text,
            event_type=InfoEventType.HIT_ENTRY,
            pair_raw=m.group(1).upper(),
            r_multiple=None,
        )

    if message_type == MessageType.INFO_CLOSE:
        m_manual = _RE_MANUAL_CLOSE.search(stripped)
        if m_manual:
            return InfoMessage(
                raw_text=text,
                event_type=InfoEventType.MANUAL_CLOSE,
                pair_raw=m_manual.group(1).upper(),
                r_multiple=float(m_manual.group(2)),
            )
        m_close = _RE_CLOSE.search(stripped)
        if m_close:
            return InfoMessage(
                raw_text=text,
                event_type=InfoEventType.CLOSE,
                pair_raw=m_close.group(1).upper(),
                r_multiple=float(m_close.group(2)),
            )
        return None

    if message_type == MessageType.INFO_RUNNING:
        m = _RE_RUNNING.search(stripped)
        if not m:
            return None
        return InfoMessage(
            raw_text=text,
            event_type=InfoEventType.RUNNING,
            pair_raw=m.group(1).upper(),
            r_multiple=float(m.group(2)),
        )

    return None


# ── Fallback direction (handler format menyimpang) ─────────────────────────

def _fallback_direction(text: str) -> Optional[str]:
    """
    Cari kata long/buy atau short/sell di luar pola header baku
    "SWING SETUP - ...". Dipakai hanya kalau direction primer (Step 4) gagal,
    supaya bot tetap fleksibel terhadap analyst yang nulis format beda
    — tapi hasil ini dapat kredit confidence lebih rendah, BUKAN penuh.
    """
    from core.constants import Direction

    has_long = _RE_FALLBACK_LONG.search(text) is not None
    has_short = _RE_FALLBACK_SHORT.search(text) is not None

    if has_long and not has_short:
        return Direction.LONG
    if has_short and not has_long:
        return Direction.SHORT
    return None  # ambigu (kedua kata muncul) atau tidak ketemu sama sekali


# ── Confidence scoring ──────────────────────────────────────────────────────

def compute_confidence(
    parsed: ParsedSignal,
    text: str,
    direction_is_fallback: bool = False,
) -> tuple:
    """
    Hitung confidence score (0-100) dari hasil ParsedSignal + struktur teks.

    Args:
        direction_is_fallback: True kalau `parsed.direction` didapat dari
            fallback (header tidak baku) — kredit lebih rendah daripada
            direction yang ketemu via pola header "SWING SETUP - ..." asli.

    Returns:
        (score, breakdown_dict) — breakdown berguna untuk debug/log/notifikasi.
    """
    breakdown: dict = {}

    # ── Direction ───────────────────────────────────────────────────────
    if parsed.direction is None:
        breakdown["direction"] = 0
    elif direction_is_fallback:
        breakdown["direction"] = _WEIGHT_DIRECTION_FALLBACK
    else:
        breakdown["direction"] = _WEIGHT_DIRECTION

    # ── Pair / symbol validity ─────────────────────────────────────────
    breakdown["pair_valid"] = _WEIGHT_PAIR_VALID if parsed.symbol_valid else 0

    # ── Entry type ──────────────────────────────────────────────────────
    breakdown["entry_type"] = _WEIGHT_ENTRY_TYPE if parsed.entry_type is not None else 0

    # ── Entry price (hanya wajib untuk limit; market dapat kredit penuh) ─
    from core.constants import EntryType
    if parsed.entry_type == EntryType.MARKET:
        breakdown["entry_price"] = _WEIGHT_ENTRY_PRICE
    elif parsed.entry_type == EntryType.LIMIT and parsed.entry_price is not None:
        breakdown["entry_price"] = _WEIGHT_ENTRY_PRICE
    else:
        breakdown["entry_price"] = 0

    # ── Stop loss ───────────────────────────────────────────────────────
    breakdown["stop_loss"] = _WEIGHT_STOP_LOSS if parsed.stop_loss is not None else 0

    # ── Template structure ─────────────────────────────────────────────
    has_header = _RE_HEADER.search(text) is not None
    bullet_count = text.count(_TEMPLATE_BULLET)
    follows_template = has_header and bullet_count >= _TEMPLATE_MIN_BULLETS
    breakdown["template_structure"] = _WEIGHT_TEMPLATE_STRUCTURE if follows_template else 0

    score = sum(breakdown.values())
    return score, breakdown


# ── Entry point gabungan ────────────────────────────────────────────────────

async def evaluate_signal(
    text: str,
    market_validator: Optional[MarketValidator] = None,
    confidence_threshold: Optional[int] = None,
) -> SignalEvaluation:
    """
    Evaluasi satu pesan mentah secara penuh: klasifikasi → (field extraction
    + confidence scoring) ATAU (parsing info-only) → keputusan akhir.

    Args:
        text: teks pesan mentah dari Telegram.
        market_validator: lihat bot/parser/signal_parser.py — di-passthrough
              ke parse_signal() untuk kandidat sinyal entry baru.
        confidence_threshold: override threshold (0-100). Default ambil dari
              settings.PARSER_CONFIDENCE_THRESHOLD, fallback 95 kalau
              settings belum bisa di-load (mis. saat unit test tanpa .env).

    Returns:
        SignalEvaluation — parse_status FINAL menentukan apa yang pipeline
        harus lakukan:
            SUCCESS   → eksekusi (lanjut ke risk engine, Step 9)
            AMBIGUOUS → JANGAN eksekusi, kirim alert manual (Step 7/18)
            INFO_ONLY → cuma log/notifikasi, tidak memicu aksi
            INVALID   → tidak dipakai di sini (reserved utk error parsing fatal)
    """
    if confidence_threshold is None:
        try:
            from config.settings import settings
            confidence_threshold = settings.PARSER_CONFIDENCE_THRESHOLD
        except Exception:
            confidence_threshold = DEFAULT_CONFIDENCE_THRESHOLD

    message_type = classify_message_type(text)

    # ── Jalur 1: pesan info-only (update/follow-up) ───────────────────────
    if message_type in (
        MessageType.INFO_HIT_ENTRY,
        MessageType.INFO_CLOSE,
        MessageType.INFO_RUNNING,
    ):
        info = parse_info_message(text, message_type)
        if info is None:
            # Match awal kepicu tapi detail gagal diparse — perlakukan ambigu
            logger.warning(
                "[ambiguity] message_type=%s tapi parse_info_message gagal — text=%r",
                message_type, text,
            )
            return SignalEvaluation(
                raw_text=text,
                message_type=MessageType.UNKNOWN,
                parse_status=ParseStatus.AMBIGUOUS,
                ambiguous_reasons=[
                    "Pesan terdeteksi mirip update posisi tapi detail "
                    "(pair/R-multiple) gagal diekstrak — perlu cek manual."
                ],
            )
        logger.info(
            "[ambiguity] Pesan info-only dikenali | event=%s | pair=%s | R=%s",
            info.event_type, info.pair_raw, info.r_multiple,
        )
        return SignalEvaluation(
            raw_text=text,
            message_type=message_type,
            parse_status=ParseStatus.INFO_ONLY,
            info=info,
        )

    # ── Jalur 2: UNKNOWN — tidak match pola apapun ─────────────────────────
    if message_type == MessageType.UNKNOWN:
        return SignalEvaluation(
            raw_text=text,
            message_type=MessageType.UNKNOWN,
            parse_status=ParseStatus.AMBIGUOUS,
            ambiguous_reasons=[
                "Pesan tidak match pola sinyal entry maupun pola update "
                "posisi (hit entry/close/running) — perlu konfirmasi manual."
            ],
        )

    # ── Jalur 3: kandidat sinyal entry baru ────────────────────────────────
    parsed = await parse_signal(text, market_validator=market_validator)

    # Fallback direction kalau primer (Step 4, pola header baku) gagal —
    # handler untuk format yang menyimpang dari template.
    used_fallback_direction = False
    if parsed.direction is None:
        fallback = _fallback_direction(text)
        if fallback is not None:
            parsed.direction = fallback
            used_fallback_direction = True
            parsed.notes.append(
                f"Direction '{fallback}' terdeteksi via fallback (header tidak "
                f"mengikuti format baku 'SWING SETUP - ...') — confidence diturunkan."
            )
            if "direction" in parsed.missing_fields:
                parsed.missing_fields.remove("direction")

    confidence, breakdown = compute_confidence(
        parsed, text, direction_is_fallback=used_fallback_direction
    )

    reasons = []
    if not parsed.symbol_valid:
        reasons.append("Pair tidak dikenali / tidak ada di market list Bitget.")
    if parsed.stop_loss is None:
        reasons.append("Tidak ada angka Stop Loss yang valid.")
    if parsed.entry_type is None:
        reasons.append("Entry type tidak jelas (bukan 'limit' atau 'market').")
    elif parsed.entry_type == "limit" and parsed.entry_price is None:
        reasons.append("Entry limit tanpa harga eksplisit.")
    if parsed.direction is None:
        reasons.append("Arah (long/short) tidak terdeteksi sama sekali.")
    if confidence < confidence_threshold:
        reasons.append(
            f"Confidence keseluruhan {confidence}% di bawah threshold "
            f"{confidence_threshold}% — kemungkinan format menyimpang dari template."
        )

    is_success = (
        confidence >= confidence_threshold
        and not parsed.missing_fields
        and parsed.symbol_valid
    )
    final_status = ParseStatus.SUCCESS if is_success else ParseStatus.AMBIGUOUS

    if final_status == ParseStatus.AMBIGUOUS:
        logger.warning(
            "[ambiguity] Sinyal AMBIGU — confidence=%s%% (threshold=%s%%) | reasons=%s",
            confidence, confidence_threshold, reasons,
        )
    else:
        logger.info(
            "[ambiguity] Sinyal SUKSES diparsing — confidence=%s%% | %s %s",
            confidence, parsed.direction, parsed.pair_normalized,
        )

    return SignalEvaluation(
        raw_text=text,
        message_type=MessageType.NEW_SIGNAL_CANDIDATE,
        parse_status=final_status,
        parsed=parsed,
        confidence=confidence,
        confidence_breakdown=breakdown,
        ambiguous_reasons=reasons,
    )
