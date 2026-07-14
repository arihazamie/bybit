"""
bot/control_bot/menu/router.py
================================
Step 1 — dispatcher untuk semua callback_data berprefix "menu:".

Tiga jenis aksi:
  1. Navigasi submenu (menu:main, menu:info, menu:risk, menu:pos, menu:ctrl,
     menu:risk:conflictmode) → edit pesan menu ke submenu tujuan.
  2. Aksi langsung zero-arg (menu:info:dashboard, menu:ctrl:pause, dst)
     → panggil fungsi cmd_* lama apa adanya (logic tidak diduplikasi).
  3. Aksi butuh input:
       a. Pilih pair dari list (menu:pos:close:pick → tombol tiap pair)
       b. Input teks bebas (menu:risk:setrisk → bot minta reply teks)
     Keduanya berakhir memanggil fungsi cmd_* lama dengan context.args
     yang disusun dari pilihan tombol / balasan teks user.

Fungsi cmd_* lama mengirim respons via update.effective_message.reply_text
(sudah disesuaikan di commands/*.py), jadi tetap jalan normal dipanggil
dari callback_query maupun dari command message biasa.
"""

from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, TimedOut
from telegram.ext import ContextTypes

from bot.control_bot.auth import authorized
from bot.control_bot.commands.info import (
    cmd_dashboard, cmd_history, cmd_positions, cmd_settings, cmd_status,
)
from bot.control_bot.commands.risk import (
    cmd_conflictmode, cmd_leverage, cmd_riskmode,
    cmd_setleverage, cmd_setmaxloss, cmd_setrisk,
)
from bot.control_bot.commands.position import (
    _reconcile_before_action,
    cmd_cancel, cmd_close, cmd_closeall, cmd_pause, cmd_pending,
    cmd_resume, cmd_setentry, cmd_setsl, cmd_settp,
)
from bot.control_bot.menu.keyboards import (
    MAIN_MENU_TEXT, conflictmode_menu_kb, ctrl_menu_kb, info_menu_kb,
    main_menu_kb, pair_pick_kb, pos_menu_kb, risk_menu_kb,
)
from bot.control_bot.menu.state import AwaitingInput, menu_state
from db.crud.trades import async_get_filled_open_trades, async_get_open_trades, async_get_pending_trades

logger = logging.getLogger(__name__)

# Aksi zero-arg → fungsi cmd_* lama, langsung dipanggil tanpa args tambahan.
_ZERO_ARG_ACTIONS = {
    ("info", "dashboard"): cmd_dashboard,
    ("info", "positions"): cmd_positions,
    ("info", "pending"):   cmd_pending,
    ("info", "history"):   cmd_history,
    ("info", "settings"):  cmd_settings,
    ("info", "status"):    cmd_status,
    ("risk", "riskmode"):  cmd_riskmode,
    ("risk", "leverage"):  cmd_leverage,
    ("pos",  "closeall"):  cmd_closeall,
    ("ctrl", "pause"):     cmd_pause,
    ("ctrl", "resume"):    cmd_resume,
}

# Aksi yang minta input teks bebas → fungsi cmd_* lama + prompt yang ditampilkan.
_TEXT_INPUT_ACTIONS = {
    ("risk", "setrisk"): (
        cmd_setrisk, "Kirim <b>persen risk</b> per trade.\nContoh: <code>1.5</code>"
    ),
    ("risk", "setmaxloss"): (
        cmd_setmaxloss, "Kirim <b>nominal max loss (USD)</b>.\nContoh: <code>5</code>"
    ),
    ("risk", "setleverage"): (
        cmd_setleverage,
        "Kirim <b>pair dan cap</b>, dipisah spasi.\n"
        "Contoh: <code>BTC/USDT:USDT 50</code>",
    ),
}

# Aksi pilih-pair dulu → (fungsi cmd_* lama, sumber daftar pair, prompt setelah pair dipilih)
_PAIR_PICK_ACTIONS = {
    "settp":    ("open_only",   cmd_settp,    "Kirim <b>harga TP</b> untuk <code>{pair}</code>."),
    "setsl":    ("open_only",   cmd_setsl,    "Kirim <b>harga SL</b> untuk <code>{pair}</code>."),
    "setentry": ("pending",     cmd_setentry, "Kirim <b>harga entry</b> baru untuk <code>{pair}</code>."),
    "close":    ("open_only",   cmd_close,    None),   # zero-arg lanjutan setelah pair dipilih
    "cancel":   ("pending",     cmd_cancel,   None),
}

_MENU_SCREENS = {
    "main": (MAIN_MENU_TEXT, main_menu_kb),
    "info": ("<b>📊 Info</b>\n\nPilih data yang ingin dilihat:", info_menu_kb),
    "risk": ("<b>⚖️ Risk & Leverage</b>\n\nPilih pengaturan:", risk_menu_kb),
    "pos":  ("<b>📌 Kelola Posisi</b>\n\nPilih aksi:", pos_menu_kb),
    "ctrl": ("<b>⏯️ Kontrol Bot</b>\n\nPilih aksi:", ctrl_menu_kb),
    "risk:conflictmode": (
        "<b>⚔️ Conflict Mode</b>\n\nPilih mode saat sinyal bentrok posisi:",
        conflictmode_menu_kb,
    ),
}


async def _show_screen(update: Update, screen_key: str) -> None:
    text, kb_fn = _MENU_SCREENS[screen_key]
    query = update.callback_query
    try:
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb_fn())
    except BadRequest as exc:
        if "not modified" not in str(exc).lower():
            raise


async def _cancel_prompt_kb(return_to: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Batal", callback_data=return_to)]])


async def _prompt_pair_pick(update: Update, action: str) -> None:
    query = update.callback_query
    if not await _reconcile_before_action():
        await query.edit_message_text(
            "❌ Gagal verifikasi ke exchange, coba lagi.",
            reply_markup=await _cancel_prompt_kb("menu:pos"),
        )
        return

    source, _, _ = _PAIR_PICK_ACTIONS[action]
    if source == "open_only":
        trades = await async_get_filled_open_trades()
    elif source == "open":
        trades = await async_get_open_trades()
    else:
        trades = await async_get_pending_trades()
    pairs = sorted({t["pair"] for t in trades})

    if not pairs:
        label = "posisi open" if source in ("open", "open_only") else "order pending"
        await query.edit_message_text(
            f"ℹ️ Tidak ada {label} saat ini.",
            reply_markup=await _cancel_prompt_kb("menu:pos"),
        )
        return

    await query.edit_message_text(
        "Pilih pair:",
        reply_markup=pair_pick_kb(action, pairs, back_target="menu:pos"),
    )


async def _prompt_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE,
                              handler, prompt: str, prefix_args: list[str],
                              return_menu: str) -> None:
    chat_id = update.effective_chat.id
    menu_state.set_awaiting(
        chat_id,
        AwaitingInput(handler=handler, prefix_args=prefix_args, return_menu=return_menu),
    )
    query = update.callback_query
    await query.edit_message_text(
        prompt,
        parse_mode=ParseMode.HTML,
        reply_markup=await _cancel_prompt_kb(return_menu),
    )


@authorized
async def handle_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    data = query.data or ""
    chat_id = update.effective_chat.id

    # Navigasi menu manapun membatalkan input teks yang sedang ditunggu.
    menu_state.clear(chat_id)

    try:
        await query.answer()
    except TimedOut:
        logger.warning("query.answer() timeout, lanjut proses callback: %s", data)

    parts = data.split(":", 3)  # ["menu", category, action?, extra?]
    if len(parts) < 2:
        return
    category = parts[1]
    action = parts[2] if len(parts) > 2 else None
    extra = parts[3] if len(parts) > 3 else None

    # ── Navigasi submenu ────────────────────────────────────────────────
    if category == "main":
        await _show_screen(update, "main")
        return
    if action is None:
        if category in _MENU_SCREENS:
            await _show_screen(update, category)
        return
    if category == "risk" and action == "conflictmode" and extra is None:
        await _show_screen(update, "risk:conflictmode")
        return

    # ── Conflict mode: pilihan tetap, langsung eksekusi ────────────────
    if category == "risk" and action == "conflictmode" and extra:
        context.args = [extra]
        await cmd_conflictmode(update, context)
        return

    # ── Aksi zero-arg ───────────────────────────────────────────────────
    if (category, action) in _ZERO_ARG_ACTIONS:
        context.args = []
        await _ZERO_ARG_ACTIONS[(category, action)](update, context)
        return

    # ── Aksi input teks bebas ───────────────────────────────────────────
    if (category, action) in _TEXT_INPUT_ACTIONS and extra is None:
        handler, prompt = _TEXT_INPUT_ACTIONS[(category, action)]
        parent_menu = f"menu:{category}"
        await _prompt_text_input(update, context, handler, prompt, [], parent_menu)
        return

    # ── Aksi pilih-pair ──────────────────────────────────────────────────
    if category == "pos" and action in _PAIR_PICK_ACTIONS:
        if extra == "pick":
            await _prompt_pair_pick(update, action)
            return
        if extra:
            pair = extra
            _, handler, next_prompt = _PAIR_PICK_ACTIONS[action]
            if next_prompt is None:
                # Contoh: close/cancel — langsung eksekusi dengan pair terpilih
                # (fungsi lama sudah menampilkan konfirmasi Y/N via pos: callback)
                context.args = [pair]
                await handler(update, context)
                return
            await _prompt_text_input(
                update, context, handler, next_prompt.format(pair=pair),
                [pair], "menu:pos",
            )
            return

    logger.warning("menu callback tidak dikenali: %s", data)


@authorized
async def handle_awaited_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handler untuk pesan teks biasa (bukan command) — dipakai untuk menangkap
    balasan user setelah bot minta input (lihat _prompt_text_input di atas).
    Tidak melakukan apa-apa kalau tidak ada state yang sedang ditunggu, supaya
    tidak mengganggu pesan lain di chat.
    """
    chat_id = update.effective_chat.id
    awaiting = menu_state.get_awaiting(chat_id)
    if awaiting is None:
        return

    menu_state.clear(chat_id)
    text = (update.effective_message.text or "").strip()
    context.args = [*awaiting.prefix_args, *text.split()]
    await awaiting.handler(update, context)