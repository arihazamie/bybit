"""
bot/control_bot/inline/conflict_confirm.py
============================================
Inline keyboard confirmation untuk konflik posisi.

Konflik OPEN  → [➕ Tambah] [🚫 Abaikan] [🔄 Replace]
Konflik PENDING → baris 1: [➕ Tambah] [🚫 Abaikan]
                   baris 2: [🔄 Replace] [❌ Cancel pending]

Timeout → auto-abaikan + edit pesan.

Executor callbacks (step 19 register via set_conflict_fns):
  AddPositionFn     = async (new_signal_data, existing_trade) → str
  ReplacePositionFn = async (new_signal_data, existing_trade) → str
  CancelPendingFn   = async (pair, existing_trade) → str
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Optional

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from bot.control_bot.inline.pending_store import make_pending_key, pending_store
from config.settings import settings

logger = logging.getLogger(__name__)

CALLBACK_PREFIX = "conf"

AddPositionFn     = Callable[[dict, dict], Awaitable[str]]
ReplacePositionFn = Callable[[dict, dict], Awaitable[str]]
CancelPendingFn   = Callable[[str, dict], Awaitable[str]]

_add_fn:     Optional[AddPositionFn]     = None
_replace_fn: Optional[ReplacePositionFn] = None
_cancel_fn:  Optional[CancelPendingFn]   = None


def set_conflict_fns(
    add_fn:     AddPositionFn,
    replace_fn: ReplacePositionFn,
    cancel_fn:  CancelPendingFn,
) -> None:
    """Step 19 panggil ini untuk wire executor ke conflict actions."""
    global _add_fn, _replace_fn, _cancel_fn
    _add_fn, _replace_fn, _cancel_fn = add_fn, replace_fn, cancel_fn


def _kb_open(key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("➕ Tambah",   callback_data=f"{CALLBACK_PREFIX}:{key}:add"),
        InlineKeyboardButton("🚫 Abaikan",  callback_data=f"{CALLBACK_PREFIX}:{key}:skip"),
        InlineKeyboardButton("🔄 Replace",  callback_data=f"{CALLBACK_PREFIX}:{key}:replace"),
    ]])


def _kb_pending(key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("➕ Tambah",   callback_data=f"{CALLBACK_PREFIX}:{key}:add"),
            InlineKeyboardButton("🚫 Abaikan",  callback_data=f"{CALLBACK_PREFIX}:{key}:skip"),
        ],
        [
            InlineKeyboardButton("🔄 Replace",        callback_data=f"{CALLBACK_PREFIX}:{key}:replace"),
            InlineKeyboardButton("❌ Cancel pending",  callback_data=f"{CALLBACK_PREFIX}:{key}:cancel_pending"),
        ],
    ])


def _fmt(val: Any, suffix: str = "") -> str:
    if val is None:
        return "?"
    try:
        return f"{val:g}{suffix}"
    except (TypeError, ValueError):
        return str(val)


async def send_conflict_confirm(
    bot: Bot,
    chat_id: int | str,
    pair: str,
    existing_trade: dict,
    new_signal_data: dict,
    conflict_type: str,   # "open" | "pending"
) -> None:
    """
    Kirim pesan konfirmasi konflik posisi ke control chat.
    conflict_type: "open" = sudah ada posisi open,
                   "pending" = sudah ada pending order.
    """
    key         = make_pending_key()
    timeout_sec = settings.CONFIRMATION_TIMEOUT_MINUTES * 60

    # Existing trade info
    direction  = (existing_trade.get("direction") or "?").upper()
    trade_id   = existing_trade.get("id", "?")
    entry_str  = _fmt(existing_trade.get("entry_price"))
    sl_str     = _fmt(existing_trade.get("sl_price"))

    # New signal info
    new_dir        = (new_signal_data.get("direction") or "?").upper()
    new_entry      = new_signal_data.get("entry_price")
    new_sl         = new_signal_data.get("sl_price")
    new_type       = new_signal_data.get("entry_type", "?")
    new_entry_str  = _fmt(new_entry) if new_entry else "market"
    new_sl_str     = _fmt(new_sl)

    conflict_label = "posisi OPEN" if conflict_type == "open" else "pending ORDER"
    kb = _kb_open(key) if conflict_type == "open" else _kb_pending(key)

    text = (
        f"🔁 <b>Konflik Posisi — Sinyal Baru Masuk</b>\n\n"
        f"Pair: <code>{pair}</code>\n\n"
        f"<b>Existing {conflict_label}:</b>\n"
        f"  Arah  : {direction} | Trade #{trade_id}\n"
        f"  Entry : <code>{entry_str}</code> | SL : <code>{sl_str}</code>\n\n"
        f"<b>Sinyal baru:</b>\n"
        f"  Arah  : {new_dir} | Type : {new_type}\n"
        f"  Entry : <code>{new_entry_str}</code> | SL : <code>{new_sl_str}</code>\n\n"
        f"<i>⏰ Auto-abaikan dalam {settings.CONFIRMATION_TIMEOUT_MINUTES} menit</i>"
    )

    sent      = await bot.send_message(
        chat_id=chat_id, text=text, parse_mode=ParseMode.HTML, reply_markup=kb
    )
    tg_msg_id = sent.message_id

    async def on_timeout(k: str, _payload: dict) -> None:
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=tg_msg_id,
                text=(
                    f"⏰ <b>Konfirmasi konflik kedaluwarsa</b> — sinyal baru diabaikan otomatis.\n\n"
                    f"Pair: <code>{pair}</code>"
                ),
                parse_mode=ParseMode.HTML,
            )
        except Exception as exc:
            logger.warning("[conflict_confirm] timeout edit error: %s", exc)

    pending_store.add(
        key,
        payload={
            "tg_msg_id":       tg_msg_id,
            "chat_id":         chat_id,
            "pair":            pair,
            "existing_trade":  existing_trade,
            "new_signal_data": new_signal_data,
            "conflict_type":   conflict_type,
        },
        timeout_seconds=timeout_sec,
        on_timeout=on_timeout,
    )
    logger.info("[conflict_confirm] pending key=%s pair=%s type=%s", key, pair, conflict_type)


async def handle_conflict_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """CallbackQueryHandler untuk prefix 'conf:'."""
    query = update.callback_query
    await query.answer()

    parts = (query.data or "").split(":")
    if len(parts) != 3:
        return

    _, key, action = parts
    payload = pending_store.pop(key)

    if payload is None:
        await query.edit_message_text(
            "⚠️ Konfirmasi sudah kedaluwarsa atau sudah diproses.",
            parse_mode=ParseMode.HTML,
        )
        return

    pair           = payload["pair"]
    existing_trade = payload["existing_trade"]
    new_signal_data = payload["new_signal_data"]

    if action == "skip":
        logger.info("[conflict_confirm] key=%s → diabaikan", key)
        await query.edit_message_text(
            f"🚫 <b>Sinyal baru diabaikan.</b>\n\nPair: <code>{pair}</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    _ACTION_MAP = {
        "add":            (_add_fn,     "Tambah posisi",      (new_signal_data, existing_trade)),
        "replace":        (_replace_fn, "Replace posisi",     (new_signal_data, existing_trade)),
        "cancel_pending": (_cancel_fn,  "Cancel pending order", (pair, existing_trade)),
    }

    if action not in _ACTION_MAP:
        await query.edit_message_text(
            f"❌ Aksi tidak dikenali: <code>{action}</code>", parse_mode=ParseMode.HTML
        )
        return

    fn, label, args = _ACTION_MAP[action]
    if fn is None:
        await query.edit_message_text(
            f"⚠️ Executor untuk '{label}' belum terdaftar (akan di-wire di step 19).",
            parse_mode=ParseMode.HTML,
        )
        return

    await query.edit_message_text(f"⏳ {label}...", parse_mode=ParseMode.HTML)
    try:
        result_text = await fn(*args)
    except Exception as exc:
        logger.exception("[conflict_confirm] %s error: %s", label, exc)
        result_text = f"❌ Error {label}: {exc}"

    try:
        await query.edit_message_text(result_text, parse_mode=ParseMode.HTML)
    except Exception:
        pass
