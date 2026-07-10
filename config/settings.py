"""
config/settings.py
==================
Load semua environment variable dari .env dan validasi saat startup.
Gagal keras (raise ValueError) jika ada env var WAJIB yang kosong/tidak ada —
lebih baik crash saat startup daripada diam-diam salah saat trading.

Gunakan:
    from config.settings import settings
    print(settings.BITGET_API_KEY)
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# Load .env dari root project (satu level di atas folder config/)
_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_ROOT / ".env")


# ── Helper ─────────────────────────────────────────────────────────────

def _require(key: str) -> str:
    """Ambil env var; raise ValueError jika tidak ada atau kosong."""
    val = os.getenv(key, "").strip()
    if not val:
        raise ValueError(
            f"[CONFIG] Environment variable '{key}' WAJIB diisi tapi kosong. "
            f"Salin .env.example → .env dan isi nilainya."
        )
    return val


def _optional(key: str, default: str = "") -> str:
    """Ambil env var opsional; kembalikan default jika tidak ada."""
    return os.getenv(key, default).strip()


def _optional_int(key: str, default: int) -> int:
    raw = os.getenv(key, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ValueError(
            f"[CONFIG] '{key}' harus berupa integer, dapat: '{raw}'"
        )


def _optional_float(key: str, default: float) -> float:
    raw = os.getenv(key, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        raise ValueError(
            f"[CONFIG] '{key}' harus berupa float, dapat: '{raw}'"
        )


def _optional_bool(key: str, default: bool) -> bool:
    raw = os.getenv(key, "").strip().lower()
    if not raw:
        return default
    if raw in ("true", "1", "yes"):
        return True
    if raw in ("false", "0", "no"):
        return False
    raise ValueError(
        f"[CONFIG] '{key}' harus true/false, dapat: '{raw}'"
    )


# ── Settings dataclass ──────────────────────────────────────────────────

@dataclass(frozen=True)
class Settings:
    """
    Immutable container untuk seluruh konfigurasi bot.
    Dibuat sekali saat startup; semua komponen import dari sini.
    """

    # ── Telegram: Akun Pribadi (Telethon) ──────────────────────────────
    TELEGRAM_API_ID: int = field(default=0)
    TELEGRAM_API_HASH: str = field(default="")
    TELEGRAM_PHONE: str = field(default="")

    # ── Telegram: Control Bot ───────────────────────────────────────────
    TELEGRAM_BOT_TOKEN: str = field(default="")
    TELEGRAM_CONTROL_CHAT_ID: str = field(default="")

    # ── Bitget API ──────────────────────────────────────────────────────
    BITGET_API_KEY: str = field(default="")
    BITGET_API_SECRET: str = field(default="")
    BITGET_PASSPHRASE: str = field(default="")
    BITGET_USE_SANDBOX: bool = field(default=True)

    # ── Signal Source ───────────────────────────────────────────────────
    SIGNAL_GROUP_NAME: str = field(default="TRADING HUB | VIP CC")
    SIGNAL_TOPIC_ID: int = field(default=0)

    # ── Risk Defaults ───────────────────────────────────────────────────
    DEFAULT_RISK_MODE: str = field(default="percent")       # "percent" | "fixed_usd"
    DEFAULT_RISK_PERCENT: float = field(default=1.0)
    DEFAULT_MAX_LOSS_USD: float = field(default=5.0)

    # ── Bot Behavior ────────────────────────────────────────────────────
    DRY_RUN: bool = field(default=True)
    DEFAULT_CONFLICT_MODE: str = field(default="ask")       # ask|skip|add|replace
    PARSER_CONFIDENCE_THRESHOLD: int = field(default=95)    # 0–100
    CONFIRMATION_TIMEOUT_MINUTES: int = field(default=10)   # timeout konfirmasi inline button

    # ── Circuit Breaker ─────────────────────────────────────────────────
    CIRCUIT_BREAKER_ERROR_THRESHOLD: int = field(default=3)
    CIRCUIT_BREAKER_WINDOW_MINUTES: int = field(default=5)

    # ── Leverage & Safety ───────────────────────────────────────────────
    LIQUIDATION_BUFFER_PERCENT: float = field(default=0.05) # 5%

    # ── Logging ─────────────────────────────────────────────────────────
    LOG_LEVEL: str = field(default="INFO")
    LOG_DIR: str = field(default="logs")
    LOG_MAX_BYTES: int = field(default=10_485_760)           # 10 MB
    LOG_BACKUP_COUNT: int = field(default=5)

    # ── Database ────────────────────────────────────────────────────────
    DB_PATH: str = field(default="data/bot.db")

    # ── Timezone ────────────────────────────────────────────────────────
    DISPLAY_TIMEZONE: str = field(default="Asia/Jakarta")


def _load_settings() -> Settings:
    """
    Baca semua env var, validasi yang wajib, dan kembalikan Settings.
    Dipanggil sekali saat modul pertama kali di-import.
    """
    # ── Validasi env var WAJIB ─────────────────────────────────────────
    # Semua key di bawah ini WAJIB ada di .env — bot tidak akan start tanpa ini.
    required_keys = [
        "TELEGRAM_API_ID",
        "TELEGRAM_API_HASH",
        "TELEGRAM_PHONE",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CONTROL_CHAT_ID",
        "BITGET_API_KEY",
        "BITGET_API_SECRET",
        "BITGET_PASSPHRASE",
    ]
    errors = []
    for key in required_keys:
        val = os.getenv(key, "").strip()
        if not val:
            errors.append(key)

    if errors:
        raise ValueError(
            "[CONFIG] Bot tidak bisa start — env var berikut WAJIB diisi di .env:\n"
            + "\n".join(f"  • {k}" for k in errors)
            + "\n\nSalin .env.example → .env dan isi semua value yang diperlukan."
        )

    # ── Validasi SIGNAL_TOPIC_ID ────────────────────────────────────────
    topic_id_raw = os.getenv("SIGNAL_TOPIC_ID", "").strip()
    if not topic_id_raw:
        raise ValueError(
            "[CONFIG] 'SIGNAL_TOPIC_ID' wajib diisi — ini adalah ID numerik "
            "topic [FUTURES] - Signals di grup Telegram."
        )
    try:
        signal_topic_id = int(topic_id_raw)
    except ValueError:
        raise ValueError(
            f"[CONFIG] 'SIGNAL_TOPIC_ID' harus berupa integer, dapat: '{topic_id_raw}'"
        )

    # ── Validasi risk mode ──────────────────────────────────────────────
    risk_mode = _optional("DEFAULT_RISK_MODE", "percent").lower()
    if risk_mode not in ("percent", "fixed_usd"):
        raise ValueError(
            f"[CONFIG] 'DEFAULT_RISK_MODE' harus 'percent' atau 'fixed_usd', dapat: '{risk_mode}'"
        )

    # ── Validasi conflict mode ──────────────────────────────────────────
    conflict_mode = _optional("DEFAULT_CONFLICT_MODE", "ask").lower()
    if conflict_mode not in ("ask", "skip", "add", "replace"):
        raise ValueError(
            f"[CONFIG] 'DEFAULT_CONFLICT_MODE' harus ask/skip/add/replace, dapat: '{conflict_mode}'"
        )

    # ── Build Settings ──────────────────────────────────────────────────
    return Settings(
        # Telegram akun pribadi
        TELEGRAM_API_ID=int(_require("TELEGRAM_API_ID")),
        TELEGRAM_API_HASH=_require("TELEGRAM_API_HASH"),
        TELEGRAM_PHONE=_require("TELEGRAM_PHONE"),

        # Control bot
        TELEGRAM_BOT_TOKEN=_require("TELEGRAM_BOT_TOKEN"),
        TELEGRAM_CONTROL_CHAT_ID=_require("TELEGRAM_CONTROL_CHAT_ID"),

        # Bitget
        BITGET_API_KEY=_require("BITGET_API_KEY"),
        BITGET_API_SECRET=_require("BITGET_API_SECRET"),
        BITGET_PASSPHRASE=_require("BITGET_PASSPHRASE"),
        BITGET_USE_SANDBOX=_optional_bool("BITGET_USE_SANDBOX", True),

        # Signal source
        SIGNAL_GROUP_NAME=_optional("SIGNAL_GROUP_NAME", "TRADING HUB | VIP CC"),
        SIGNAL_TOPIC_ID=signal_topic_id,

        # Risk
        DEFAULT_RISK_MODE=risk_mode,
        DEFAULT_RISK_PERCENT=_optional_float("DEFAULT_RISK_PERCENT", 1.0),
        DEFAULT_MAX_LOSS_USD=_optional_float("DEFAULT_MAX_LOSS_USD", 5.0),

        # Bot behavior
        DRY_RUN=_optional_bool("DRY_RUN", True),
        DEFAULT_CONFLICT_MODE=conflict_mode,
        PARSER_CONFIDENCE_THRESHOLD=_optional_int("PARSER_CONFIDENCE_THRESHOLD", 95),
        CONFIRMATION_TIMEOUT_MINUTES=_optional_int("CONFIRMATION_TIMEOUT_MINUTES", 10),

        # Circuit breaker
        CIRCUIT_BREAKER_ERROR_THRESHOLD=_optional_int("CIRCUIT_BREAKER_ERROR_THRESHOLD", 3),
        CIRCUIT_BREAKER_WINDOW_MINUTES=_optional_int("CIRCUIT_BREAKER_WINDOW_MINUTES", 5),

        # Leverage & safety
        LIQUIDATION_BUFFER_PERCENT=_optional_float("LIQUIDATION_BUFFER_PERCENT", 0.05),

        # Logging
        LOG_LEVEL=_optional("LOG_LEVEL", "INFO").upper(),
        LOG_DIR=_optional("LOG_DIR", "logs"),
        LOG_MAX_BYTES=_optional_int("LOG_MAX_BYTES", 10_485_760),
        LOG_BACKUP_COUNT=_optional_int("LOG_BACKUP_COUNT", 5),

        # Database
        DB_PATH=_optional("DB_PATH", "data/bot.db"),

        # Timezone
        DISPLAY_TIMEZONE=_optional("DISPLAY_TIMEZONE", "Asia/Jakarta"),
    )


# ── Singleton ───────────────────────────────────────────────────────────
# Di-load saat modul pertama kali di-import.
# Kalau .env belum ada atau ada key yang kosong → raise ValueError di sini.
# Komponen lain cukup: from config.settings import settings
try:
    settings: Settings = _load_settings()
except ValueError:
    # Re-raise supaya pesan error jelas terlihat di log startup
    raise
