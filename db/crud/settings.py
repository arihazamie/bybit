"""
db/crud/settings.py
CRUD functions untuk tabel `settings`.

Settings adalah key-value store untuk konfigurasi bot yang bisa diubah
lewat command Telegram. Semua fungsi tersedia dalam versi sync dan async.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from db.database import get_db, row_to_dict, rows_to_dicts
from db.models import DEFAULT_SETTINGS

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# Sync CRUD
# ─────────────────────────────────────────────

def get_setting(key: str) -> Optional[str]:
    """
    Ambil nilai setting berdasarkan key.
    Return None jika key tidak ada.
    """
    with get_db() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else None


def get_all_settings() -> dict[str, str]:
    """Ambil semua settings sebagai dict {key: value}."""
    with get_db() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
        return {r["key"]: r["value"] for r in rows}


def set_setting(key: str, value: Any) -> None:
    """
    Set nilai setting. Upsert (INSERT OR REPLACE).
    value di-cast ke string secara otomatis.
    """
    now_utc = datetime.now(timezone.utc).isoformat()
    value_str = str(value)

    with get_db() as conn:
        conn.execute(
            """INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
            (key, value_str, now_utc),
        )

    logger.info(f"Setting updated: {key} = {value_str}")


def set_settings_batch(updates: dict[str, Any]) -> None:
    """Set multiple settings sekaligus dalam satu transaksi."""
    now_utc = datetime.now(timezone.utc).isoformat()

    with get_db() as conn:
        for key, value in updates.items():
            conn.execute(
                """INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
                (key, str(value), now_utc),
            )

    logger.info(f"Batch settings updated: {list(updates.keys())}")


def reset_setting_to_default(key: str) -> bool:
    """
    Reset satu setting ke nilai default.
    Return True jika berhasil, False jika key tidak ada di default.
    """
    if key not in DEFAULT_SETTINGS:
        logger.warning(f"No default found for setting key: {key}")
        return False

    set_setting(key, DEFAULT_SETTINGS[key])
    logger.info(f"Setting reset to default: {key} = {DEFAULT_SETTINGS[key]}")
    return True


# ─────────────────────────────────────────────
# Typed getters (convenience)
# ─────────────────────────────────────────────

def get_risk_mode() -> str:
    """Return 'percent' atau 'fixed_usd'."""
    return get_setting("risk_mode") or "percent"


def get_risk_percent() -> float:
    """Return risk % per trade (mode percent)."""
    val = get_setting("risk_percent")
    return float(val) if val else 1.0


def get_max_loss_usd() -> float:
    """Return max loss USD per trade (mode fixed_usd)."""
    val = get_setting("max_loss_usd")
    return float(val) if val else 5.0


def get_risk_amount_config() -> tuple[str, float]:
    """
    Return (mode, amount) sesuai mode aktif.
    mode: 'percent' atau 'fixed_usd'
    amount: nilai numerik yang sesuai
    """
    mode = get_risk_mode()
    if mode == "fixed_usd":
        return ("fixed_usd", get_max_loss_usd())
    return ("percent", get_risk_percent())


def is_bot_paused() -> bool:
    """Return True jika bot dalam mode pause."""
    val = get_setting("bot_paused")
    return val.lower() == "true" if val else True


def set_bot_paused(paused: bool) -> None:
    """Set status pause bot."""
    set_setting("bot_paused", "true" if paused else "false")


def get_position_conflict_mode() -> str:
    """Return mode konflik posisi: skip/ask/add/replace."""
    return get_setting("position_conflict_mode") or "ask"


def get_liquidation_buffer_pct() -> float:
    """Return buffer % untuk safety check liquidation vs SL."""
    val = get_setting("liquidation_buffer_pct")
    return float(val) if val else 5.0


def get_cb_thresholds() -> tuple[int, int]:
    """
    Return (error_threshold, window_minutes) untuk circuit breaker.
    """
    threshold = get_setting("cb_error_threshold")
    window = get_setting("cb_window_minutes")
    return (int(threshold) if threshold else 3, int(window) if window else 5)


def is_auto_execute() -> bool:
    """Return True jika auto execute aktif (tanpa konfirmasi manual)."""
    val = get_setting("auto_execute_mode")
    return val.lower() == "true" if val else False


def get_leverage_cap(pair: Optional[str] = None) -> Optional[float]:
    """
    Return cap leverage untuk pair tertentu, atau global cap jika ada.
    Return None jika tidak ada cap (bot pakai max dari exchange).

    Urutan prioritas:
    1. Pair-specific cap (key: lev_cap:{pair})
    2. Global cap (key: default_leverage_cap)
    3. None → tidak ada cap
    """
    if pair:
        pair_key = f"lev_cap:{pair}"
        val = get_setting(pair_key)
        if val and val.strip():
            return float(val)

    val = get_setting("default_leverage_cap")
    if val and val.strip():
        return float(val)
    return None


def set_leverage_cap(pair: str, cap: float) -> None:
    """Set cap leverage untuk pair tertentu. cap=0 → hapus cap."""
    pair_key = f"lev_cap:{pair}"
    if cap <= 0:
        # Hapus cap (set ke string kosong = tidak ada cap)
        set_setting(pair_key, "")
    else:
        set_setting(pair_key, str(cap))


def get_all_leverage_caps() -> dict[str, float]:
    """
    Return semua pair-specific leverage cap sebagai {pair: cap}.
    Termasuk global cap di key 'global' jika ada.
    """
    all_s = get_all_settings()
    caps: dict[str, float] = {}
    for key, val in all_s.items():
        if key.startswith("lev_cap:") and val and val.strip():
            pair = key[len("lev_cap:"):]
            try:
                caps[pair] = float(val)
            except ValueError:
                pass
    global_cap = all_s.get("default_leverage_cap", "")
    if global_cap and global_cap.strip():
        try:
            caps["_global"] = float(global_cap)
        except ValueError:
            pass
    return caps


# ─────────────────────────────────────────────
# Async versions
# ─────────────────────────────────────────────

async def async_get_setting(key: str) -> Optional[str]:
    """Async version of get_setting."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, get_setting, key)


async def async_set_setting(key: str, value: Any) -> None:
    """Async version of set_setting."""
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, set_setting, key, value)


async def async_get_all_settings() -> dict[str, str]:
    """Async version of get_all_settings."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, get_all_settings)


async def async_get_risk_mode() -> str:
    return await async_get_setting("risk_mode") or "percent"


async def async_is_bot_paused() -> bool:
    val = await async_get_setting("bot_paused")
    return val.lower() == "true" if val else True


async def async_set_bot_paused(paused: bool) -> None:
    await async_set_setting("bot_paused", "true" if paused else "false")


async def async_get_risk_amount_config() -> tuple[str, float]:
    """Async version of get_risk_amount_config."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, get_risk_amount_config)


async def async_get_leverage_cap(pair: Optional[str] = None) -> Optional[float]:
    """Async version of get_leverage_cap."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, get_leverage_cap, pair)


async def async_get_position_conflict_mode() -> str:
    """Async version of get_position_conflict_mode."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, get_position_conflict_mode)


async def async_set_leverage_cap(pair: str, cap: float) -> None:
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, set_leverage_cap, pair, cap)


async def async_get_all_leverage_caps() -> dict[str, float]:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, get_all_leverage_caps)
