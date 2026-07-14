"""
bot/control_bot/menu/keyboards.py
===================================
Step 1 — struktur menu tombol, menggantikan daftar /command.

Skema callback_data: "menu:<kategori>:<aksi>[:<extra>]"
  menu:main                     → kembali ke menu utama
  menu:info:dashboard           → aksi langsung, zero-arg
  menu:risk:setrisk             → trigger prompt input teks (lihat router.py)
  menu:risk:conflictmode:ask    → pilihan tetap (ask/skip/add/replace)
  menu:pos:close:pick           → tampilkan daftar pair posisi open utk dipilih
  menu:pos:close:<PAIR>         → pair terpilih, lanjut ke command lama

Catatan: prefix "pos:", "sig:", "conf:" SUDAH dipakai untuk inline
confirm/konflik yang lain (lihat commands/position.py, inline/*.py).
Supaya tidak bentrok, semua callback menu baru WAJIB pakai prefix "menu:".
"""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

MAIN_MENU_TEXT = "<b>🤖 Menu Bot</b>\n\nPilih kategori:"


def main_menu_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("📊 Info", callback_data="menu:info")],
        [InlineKeyboardButton("⚖️ Risk & Leverage", callback_data="menu:risk")],
        [InlineKeyboardButton("📌 Kelola Posisi", callback_data="menu:pos")],
        [InlineKeyboardButton("⏯️ Kontrol Bot", callback_data="menu:ctrl")],
    ]
    return InlineKeyboardMarkup(rows)


def _back_row(target: str = "menu:main") -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton("⬅️ Kembali", callback_data=target)]


def info_menu_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("📋 Dashboard", callback_data="menu:info:dashboard")],
        [InlineKeyboardButton("📈 Posisi Terbuka", callback_data="menu:info:positions")],
        [InlineKeyboardButton("⏳ Order Pending", callback_data="menu:info:pending")],
        [InlineKeyboardButton("🕒 Riwayat (10 terakhir)", callback_data="menu:info:history")],
        [InlineKeyboardButton("⚙️ Pengaturan", callback_data="menu:info:settings")],
        [InlineKeyboardButton("🔌 Status", callback_data="menu:info:status")],
        _back_row(),
    ]
    return InlineKeyboardMarkup(rows)


def risk_menu_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("⚖️ Lihat Mode Risk", callback_data="menu:risk:riskmode")],
        [InlineKeyboardButton("✏️ Set Risk %", callback_data="menu:risk:setrisk")],
        [InlineKeyboardButton("✏️ Set Max Loss (USD)", callback_data="menu:risk:setmaxloss")],
        [InlineKeyboardButton("🔧 Leverage Aktif", callback_data="menu:risk:leverage")],
        [InlineKeyboardButton("✏️ Set Leverage Pair", callback_data="menu:risk:setleverage")],
        [InlineKeyboardButton("⚔️ Conflict Mode", callback_data="menu:risk:conflictmode")],
        _back_row(),
    ]
    return InlineKeyboardMarkup(rows)


def conflictmode_menu_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("❓ Ask", callback_data="menu:risk:conflictmode:ask")],
        [InlineKeyboardButton("🚫 Skip", callback_data="menu:risk:conflictmode:skip")],
        [InlineKeyboardButton("➕ Add", callback_data="menu:risk:conflictmode:add")],
        [InlineKeyboardButton("🔄 Replace", callback_data="menu:risk:conflictmode:replace")],
        _back_row("menu:risk"),
    ]
    return InlineKeyboardMarkup(rows)


def pos_menu_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("🎯 Set TP", callback_data="menu:pos:settp:pick")],
        [InlineKeyboardButton("🛑 Set SL", callback_data="menu:pos:setsl:pick")],
        [InlineKeyboardButton("📍 Set Entry", callback_data="menu:pos:setentry:pick")],
        [InlineKeyboardButton("❌ Tutup Posisi", callback_data="menu:pos:close:pick")],
        [InlineKeyboardButton("🧨 Tutup Semua Posisi", callback_data="menu:pos:closeall")],
        [InlineKeyboardButton("🚫 Batalkan Order Pending", callback_data="menu:pos:cancel:pick")],
        _back_row(),
    ]
    return InlineKeyboardMarkup(rows)


def ctrl_menu_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("⏸️ Pause", callback_data="menu:ctrl:pause")],
        [InlineKeyboardButton("▶️ Resume", callback_data="menu:ctrl:resume")],
        _back_row(),
    ]
    return InlineKeyboardMarkup(rows)


def pair_pick_kb(action: str, pairs: list[str], back_target: str = "menu:pos") -> InlineKeyboardMarkup:
    """
    Daftar tombol pair untuk dipilih (mis. sebelum /close, /setsl, /cancel).
    action → nama aksi menu, mis. "close", "setsl", "setentry", "settp", "cancel".
    """
    rows = [
        [InlineKeyboardButton(pair, callback_data=f"menu:pos:{action}:{pair}")]
        for pair in pairs
    ]
    rows.append(_back_row(back_target))
    return InlineKeyboardMarkup(rows)