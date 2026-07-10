"""
bot/control_bot/auth.py
=======================
Middleware: hanya chat ID yang ada di TELEGRAM_CONTROL_CHAT_ID yang boleh
menggunakan command bot. Semua request lain di-ignore tanpa respons.
"""

from __future__ import annotations

import logging
from functools import wraps
from typing import Callable

from telegram import Update
from telegram.ext import ContextTypes

from config.settings import settings

logger = logging.getLogger(__name__)

_ALLOWED_IDS: set[int] = set()


def _get_allowed() -> set[int]:
    global _ALLOWED_IDS
    if not _ALLOWED_IDS:
        raw = settings.TELEGRAM_CONTROL_CHAT_ID
        for part in raw.replace(",", " ").split():
            try:
                _ALLOWED_IDS.add(int(part.strip()))
            except ValueError:
                logger.warning(f"TELEGRAM_CONTROL_CHAT_ID: invalid ID '{part}'")
    return _ALLOWED_IDS


def authorized(handler: Callable) -> Callable:
    """Decorator — tolak update dari chat ID yang tidak diizinkan."""
    @wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id if update.effective_chat else None
        if chat_id not in _get_allowed():
            logger.warning(f"Unauthorized access attempt from chat_id={chat_id}")
            return
        return await handler(update, context)
    return wrapper
