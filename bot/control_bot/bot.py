"""
bot/control_bot/bot.py
========================
Setup Application dan registrasi semua handler.

Step 15: info commands (dashboard, positions, history, settings, status)
Step 16: risk & leverage commands
Step 17: position management commands + pos: callback
Step 18: sig: (sinyal ambigu) dan conf: (konflik posisi) callback handlers
"""

from __future__ import annotations

import logging

from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters,
)

from bot.control_bot.commands.info import (
    cmd_dashboard, cmd_history, cmd_positions, cmd_settings, cmd_status,
)
from bot.control_bot.commands.risk import (
    cmd_conflictmode, cmd_leverage, cmd_riskmode,
    cmd_setleverage, cmd_setmaxloss, cmd_setrisk,
)
from bot.control_bot.commands.position import (
    cmd_cancel, cmd_close, cmd_closeall, cmd_pause, cmd_pending,
    cmd_resume, cmd_setentry, cmd_setsl, cmd_settp,
    handle_position_callback,
)
from bot.control_bot.inline.signal_confirm import handle_signal_callback
from bot.control_bot.inline.conflict_confirm import handle_conflict_callback
from bot.control_bot.menu.router import handle_menu_callback, handle_awaited_text
from config.settings import settings

from bot.control_bot.commands.help import cmd_help, cmd_menu, set_bot_commands

logger = logging.getLogger(__name__)


def build_application() -> Application:
    app = (
        Application.builder()
        .token(settings.TELEGRAM_BOT_TOKEN)
        .build()
    )

    # ── Step 15: Info ──────────────────────────────────────────────────
    app.add_handler(CommandHandler("dashboard", cmd_dashboard))
    app.add_handler(CommandHandler("positions", cmd_positions))
    app.add_handler(CommandHandler("history",   cmd_history))
    app.add_handler(CommandHandler("settings",  cmd_settings))
    app.add_handler(CommandHandler("status",    cmd_status))
    app.add_handler(CommandHandler("help",  cmd_help))
    app.add_handler(CommandHandler("start", cmd_menu))

    # ── Step 16: Risk & leverage ───────────────────────────────────────
    app.add_handler(CommandHandler("setrisk",      cmd_setrisk))
    app.add_handler(CommandHandler("setmaxloss",   cmd_setmaxloss))
    app.add_handler(CommandHandler("riskmode",     cmd_riskmode))
    app.add_handler(CommandHandler("setleverage",  cmd_setleverage))
    app.add_handler(CommandHandler("leverage",     cmd_leverage))
    app.add_handler(CommandHandler("conflictmode", cmd_conflictmode))

    # ── Step 17: Position management ──────────────────────────────────
    app.add_handler(CommandHandler("settp",    cmd_settp))
    app.add_handler(CommandHandler("setsl",    cmd_setsl))
    app.add_handler(CommandHandler("setentry", cmd_setentry))
    app.add_handler(CommandHandler("close",    cmd_close))
    app.add_handler(CommandHandler("closeall", cmd_closeall))
    app.add_handler(CommandHandler("pending",  cmd_pending))
    app.add_handler(CommandHandler("cancel",   cmd_cancel))
    app.add_handler(CommandHandler("pause",    cmd_pause))
    app.add_handler(CommandHandler("resume",   cmd_resume))

    # ── Step 17 callback: konfirmasi posisi (pos:) ────────────────────
    app.add_handler(CallbackQueryHandler(handle_position_callback, pattern=r"^pos:"))

    # ── Step 18 callback: sinyal ambigu (sig:) ────────────────────────
    app.add_handler(CallbackQueryHandler(handle_signal_callback, pattern=r"^sig:"))

    # ── Step 18 callback: konflik posisi (conf:) ──────────────────────
    app.add_handler(CallbackQueryHandler(handle_conflict_callback, pattern=r"^conf:"))

    # ── Step 8: menu tombol (menu:) — prefix beda, gak bentrok pos:/sig:/conf: ──
    app.add_handler(CallbackQueryHandler(handle_menu_callback, pattern=r"^menu:"))

    # ── Step 8: tangkap reply teks setelah tombol minta input ──────────
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_awaited_text))

    logger.info("Control bot built — %d handler terdaftar", len(app.handlers[0]))
    return app


async def start_control_bot() -> None:
    app = build_application()
    logger.info("Control bot starting — polling Telegram...")

    async with app:
        await app.start()
        await set_bot_commands(app)
        await app.updater.start_polling(
            allowed_updates=["message", "callback_query"],
            drop_pending_updates=True,
        )
        logger.info("Control bot polling aktif")

        try:
            import asyncio
            await asyncio.get_event_loop().create_future()
        except asyncio.CancelledError:
            pass
        finally:
            logger.info("Control bot stopping...")
            await app.updater.stop()
            await app.stop()