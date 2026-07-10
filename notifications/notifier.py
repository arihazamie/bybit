"""
notifications/notifier.py
==========================
Wrapper tipis untuk kirim notifikasi Telegram ke control chat.

Dipakai pipeline (Step 19) dan komponen lain yang perlu kirim pesan ke user
tanpa bergantung langsung ke instance Application / bot.

Pola: singleton _bot_instance di-inject saat startup (main.py),
      fungsi `notify(text)` bisa dipanggil dari mana saja secara async.
"""

from __future__ import annotations

import logging
from typing import Optional

from telegram import Bot
from telegram.constants import ParseMode

logger = logging.getLogger(__name__)

_bot: Optional[Bot] = None
_chat_id: Optional[str] = None


def init_notifier(bot: Bot, chat_id: str) -> None:
    """Panggil dari main.py setelah Application dibangun."""
    global _bot, _chat_id
    _bot = bot
    _chat_id = chat_id
    logger.info("[notifier] Initialized — chat_id=%s", chat_id)


async def notify(text: str, parse_mode: str = ParseMode.HTML) -> None:
    """Kirim teks ke control chat. Silent-fail jika bot belum diinit."""
    if _bot is None or not _chat_id:
        logger.warning("[notifier] Bot belum diinit, pesan tidak dikirim: %s", text[:80])
        return
    try:
        await _bot.send_message(chat_id=_chat_id, text=text, parse_mode=parse_mode)
    except Exception as exc:
        logger.error("[notifier] Gagal kirim notifikasi: %s", exc)


async def notify_cb_trip(component: str, reason: str) -> None:
    await notify(
        f"🔴 <b>CIRCUIT BREAKER TRIP</b>\n\n"
        f"Komponen : <code>{component}</code>\n"
        f"Alasan   : {reason}\n\n"
        f"Eksekusi sinyal baru DIHENTIKAN.\n"
        f"Kirim <code>/resume</code> setelah masalah diatasi."
    )
