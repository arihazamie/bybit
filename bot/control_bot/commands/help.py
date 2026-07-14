"""
bot/control_bot/commands/help.py
==================================
/help & /start — daftar lengkap command dikelompokkan per kategori,
supaya user tidak perlu hafal semua command satu-satu.

Catatan: daftar di sini HARUS disinkronkan manual dengan command yang
didaftarkan di bot/control_bot/bot.py (build_application) dan dengan
list BotCommand di set_bot_commands() — kalau nambah command baru,
update juga tempat ini + set_bot_commands.
"""

from __future__ import annotations

from telegram import BotCommand, Update
from telegram.constants import ParseMode
from telegram.ext import Application, ContextTypes

from bot.control_bot.auth import authorized


async def _send(update: Update, text: str) -> None:
    # effective_message tetap valid baik dipanggil dari command message
    # maupun dari tombol menu (callback_query) — Step 7.
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)


HELP_TEXT = (
    "<b>🤖 Bot Control</b>\n\n"
    "Semua aksi sekarang lewat tombol. Ketik /start untuk buka menu utama:\n"
    "📊 Info · ⚖️ Risk &amp; Leverage · 📌 Kelola Posisi · ⏯️ Kontrol Bot\n\n"
    "<i>Command lama (/dashboard, /setrisk, dst) masih jalan kalau kamu "
    "sudah hafal, tapi gak lagi muncul di daftar \"/\" Telegram.</i>"
)


@authorized
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _send(update, HELP_TEXT)


@authorized
async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Entry point tombol — dipanggil dari /start (lihat Step 9)."""
    from bot.control_bot.menu.keyboards import MAIN_MENU_TEXT, main_menu_kb

    await update.effective_message.reply_text(
        MAIN_MENU_TEXT, parse_mode=ParseMode.HTML, reply_markup=main_menu_kb()
    )


# ── Command suggestion menu bawaan Telegram (muncul saat ketik "/") ──────────
# Dipanggil sekali saat startup (lihat start_control_bot di bot.py).
# Telegram menyimpan ini di sisi server-nya sendiri, jadi tidak perlu
# dipanggil ulang tiap request — cukup tiap kali daftar command berubah.

async def set_bot_commands(app: Application) -> None:
    # Command lama tetap terdaftar di build_application() (bot.py) sebagai
    # fallback tersembunyi — cuma daftar "/" di UI Telegram yang dikecilkan
    # di sini, supaya user diarahkan pakai tombol menu (/start).
    commands = [
        BotCommand("start", "Buka menu utama (tombol)"),
        BotCommand("help",  "Info singkat & cara pakai"),
    ]
    await app.bot.set_my_commands(commands)