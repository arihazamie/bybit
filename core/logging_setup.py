"""
core/logging_setup.py
=====================
Setup logging terpusat untuk seluruh bot.

Fitur:
- Output ke console (stdout) + file (dengan rotation)
- Format lengkap: timestamp UTC, level, nama logger, pesan
- Log level dikonfigurasi dari settings
- File log disimpan di LOG_DIR dengan rotation otomatis

Gunakan:
    from core.logging_setup import setup_logging, get_logger

    setup_logging()                    # panggil sekali di main.py
    logger = get_logger(__name__)      # di tiap modul
    logger.info("Pesan")
"""

import logging
import logging.handlers
import sys
from pathlib import Path


def setup_logging(
    log_level: str = "INFO",
    log_dir: str = "logs",
    max_bytes: int = 10_485_760,
    backup_count: int = 5,
) -> None:
    """
    Inisialisasi logging untuk seluruh aplikasi.

    Args:
        log_level:    Level log (DEBUG/INFO/WARNING/ERROR/CRITICAL)
        log_dir:      Direktori penyimpanan file log (relatif dari CWD)
        max_bytes:    Ukuran max file log sebelum rotation (default 10 MB)
        backup_count: Jumlah file backup yang disimpan (default 5)
    """
    # Buat direktori log kalau belum ada
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    numeric_level = getattr(logging, log_level.upper(), logging.INFO)

    # ── Format ──────────────────────────────────────────────────────────
    # Semua timestamp pakai UTC
    formatter = logging.Formatter(
        fmt="%(asctime)s UTC | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # Paksa formatter pakai UTC (bukan localtime)
    formatter.converter = __import__("time").gmtime

    # ── Handler: Console ────────────────────────────────────────────────
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(numeric_level)
    console_handler.setFormatter(formatter)

    # ── Handler: File (rotating) ────────────────────────────────────────
    # bot.log → dirotasi otomatis saat mencapai max_bytes
    file_handler = logging.handlers.RotatingFileHandler(
        filename=log_path / "bot.log",
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)  # file selalu simpan DEBUG ke atas
    file_handler.setFormatter(formatter)

    # ── Handler: Error file terpisah ───────────────────────────────────
    # error.log hanya menyimpan WARNING ke atas — mudah di-monitor
    error_handler = logging.handlers.RotatingFileHandler(
        filename=log_path / "error.log",
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    error_handler.setLevel(logging.WARNING)
    error_handler.setFormatter(formatter)

    # ── Root logger ─────────────────────────────────────────────────────
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)  # root selalu DEBUG; handler yang filter
    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)
    root_logger.addHandler(error_handler)

    # ── Reduksi noise dari library eksternal ────────────────────────────
    logging.getLogger("telethon").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)
    logging.getLogger("ccxt").setLevel(logging.WARNING)

    # Log pertama untuk konfirmasi setup berhasil
    logger = logging.getLogger(__name__)
    logger.info(
        "Logging diinisialisasi | level=%s | log_dir=%s | "
        "bot.log (rotating %s MB × %s backup) | error.log (WARNING+)",
        log_level,
        log_path.resolve(),
        round(max_bytes / 1_048_576, 1),
        backup_count,
    )


def get_logger(name: str) -> logging.Logger:
    """
    Shortcut untuk mendapatkan logger bernama.

    Gunakan `__name__` sebagai name agar nama modul otomatis muncul di log.

    Contoh:
        logger = get_logger(__name__)
        logger.info("Telethon listener started")
    """
    return logging.getLogger(name)
