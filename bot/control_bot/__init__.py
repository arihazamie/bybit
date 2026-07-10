"""
bot/control_bot
================
Telegram control bot — terima command user, kirim notifikasi, kelola bot.

Ekspor utama:
    start_control_bot()  → coroutine, dipakai sebagai asyncio.create_task()
    build_application()  → buat Application tanpa start (untuk testing)
"""

from bot.control_bot.bot import build_application, start_control_bot

__all__ = ["start_control_bot", "build_application"]
