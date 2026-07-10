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
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


HELP_TEXT = (
    "<b>🤖 Daftar Command Bot</b>\n\n"

    "<b>📊 Info</b>\n"
    "/dashboard — Ringkasan saldo, posisi, P&amp;L hari ini\n"
    "/positions — Daftar posisi terbuka\n"
    "/pending — Daftar order pending (belum fill)\n"
    "/history [N] — Riwayat trade (default 10, maks 50)\n"
    "/settings — Lihat semua pengaturan aktif\n"
    "/status — Status koneksi exchange &amp; bot\n\n"

    "<b>⚖️ Risk &amp; Leverage</b>\n"
    "/riskmode — Lihat mode risiko aktif (percent/fixed)\n"
    "/setrisk {persen} — Set risk % per trade\n"
    "  <i>cth: /setrisk 1.5</i>\n"
    "/setmaxloss {nominal} — Set max loss tetap (USD)\n"
    "  <i>cth: /setmaxloss 5</i>\n"
    "/leverage — Cek leverage aktif semua pair\n"
    "/setleverage {pair} {cap} — Set cap leverage per pair\n"
    "  <i>cth: /setleverage BTC/USDT:USDT 50</i>\n"
    "/conflictmode [mode] — Atur aksi saat sinyal bentrok posisi\n"
    "  <i>pilihan: ask, skip, add, replace</i>\n\n"

    "<b>📌 Kelola Posisi</b>\n"
    "/settp {pair} {harga} — Set take profit (dicatat di DB saja)\n"
    "  <i>cth: /settp ETH/USDT:USDT 3600</i>\n"
    "/setsl {pair} {harga} — Update stop loss (kirim order baru ke exchange)\n"
    "  <i>cth: /setsl ETH/USDT:USDT 3380</i>\n"
    "/setentry {pair} {harga} — Update entry price tercatat\n"
    "  <i>cth: /setentry ETH/USDT:USDT 3450</i>\n"
    "/close {pair} — Tutup satu posisi\n"
    "  <i>cth: /close ETH/USDT:USDT</i>\n"
    "/closeall — Tutup SEMUA posisi terbuka\n"
    "/cancel {pair} — Batalkan order pending (belum fill)\n"
    "  <i>cth: /cancel ETH/USDT:USDT</i>\n\n"

    "<b>⏯️ Kontrol Bot</b>\n"
    "/pause — Jeda bot, sinyal baru diabaikan\n"
    "/resume — Lanjutkan bot dari mode pause\n\n"

    "<i>Tip: ketik \"/\" di chat ini untuk lihat semua command langsung "
    "dari menu Telegram, lengkap dengan deskripsi singkatnya.</i>"
)


@authorized
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _send(update, HELP_TEXT)


# ── Command suggestion menu bawaan Telegram (muncul saat ketik "/") ──────────
# Dipanggil sekali saat startup (lihat start_control_bot di bot.py).
# Telegram menyimpan ini di sisi server-nya sendiri, jadi tidak perlu
# dipanggil ulang tiap request — cukup tiap kali daftar command berubah.

async def set_bot_commands(app: Application) -> None:
    commands = [
        BotCommand("help",         "Tampilkan daftar semua command"),
        BotCommand("dashboard",    "Ringkasan saldo & posisi"),
        BotCommand("positions",    "Daftar posisi terbuka"),
        BotCommand("pending",      "Daftar order pending"),
        BotCommand("history",      "Riwayat trade terakhir"),
        BotCommand("settings",     "Lihat pengaturan aktif"),
        BotCommand("status",       "Status koneksi & bot"),
        BotCommand("riskmode",     "Lihat mode risiko aktif"),
        BotCommand("setrisk",      "Set risk % per trade"),
        BotCommand("setmaxloss",   "Set max loss tetap (USD)"),
        BotCommand("leverage",     "Cek leverage aktif"),
        BotCommand("setleverage",  "Set cap leverage per pair"),
        BotCommand("conflictmode", "Atur mode konflik posisi"),
        BotCommand("settp",        "Set take profit posisi"),
        BotCommand("setsl",        "Update stop loss posisi"),
        BotCommand("setentry",     "Update entry price posisi"),
        BotCommand("close",        "Tutup satu posisi"),
        BotCommand("closeall",     "Tutup semua posisi"),
        BotCommand("cancel",       "Batalkan order pending"),
        BotCommand("pause",        "Jeda bot"),
        BotCommand("resume",       "Lanjutkan bot"),
    ]
    await app.bot.set_my_commands(commands)