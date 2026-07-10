"""
bot/control_bot/inline/signal_confirm.py
==========================================
Inline keyboard confirmation untuk sinyal AMBIGU.

3 button:
  [✅ Eksekusi]  [🚫 Abaikan]  [✏️ Edit dulu]

Timeout → auto-abaikan + edit pesan menjadi notifikasi expired.

Executor pluggable (step 19 register via set_execute_fn):
  ExecuteFn = async (SignalEvaluation) → str  (result text untuk edit_message)
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable, Optional

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from bot.control_bot.inline.pending_store import make_pending_key, pending_store
from config.settings import settings

logger = logging.getLogger(__name__)

CALLBACK_PREFIX = "sig"

ExecuteFn = Callable[[object], Awaitable[str]]  # SignalEvaluation → result text
_execute_fn: Optional[ExecuteFn] = None


def set_execute_fn(fn: ExecuteFn) -> None:
    """Step 19 panggil ini untuk wire execution pipeline ke sinyal ambigu."""
    global _execute_fn
    _execute_fn = fn


def _keyboard(key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Eksekusi",   callback_data=f"{CALLBACK_PREFIX}:{key}:exec"),
        InlineKeyboardButton("🚫 Abaikan",    callback_data=f"{CALLBACK_PREFIX}:{key}:skip"),
        InlineKeyboardButton("✏️ Edit dulu",  callback_data=f"{CALLBACK_PREFIX}:{key}:edit"),
    ]])


async def send_ambiguous_confirm(
    bot: Bot,
    chat_id: int | str,
    evaluation: object,          # SignalEvaluation — avoid circular import
    signal_message_id: Optional[int] = None,
) -> None:
    """
    Kirim pesan konfirmasi sinyal ambigu ke control chat.
    Timeout → edit pesan lama jadi notif expired & auto-abaikan.
    """
    raw: str             = getattr(evaluation, "raw_text", "") or ""
    reasons: list        = getattr(evaluation, "ambiguous_reasons", []) or []
    confidence: Optional[int] = getattr(evaluation, "confidence", None)

    key = make_pending_key()
    timeout_sec = settings.CONFIRMATION_TIMEOUT_MINUTES * 60

    reasons_block = ""
    if reasons:
        bullet = "\n".join(f"  • {r}" for r in reasons)
        reasons_block = f"\n\n<b>Alasan ambigu:</b>\n{bullet}"

    confidence_str = f"\nConfidence : <code>{confidence}%</code>" if confidence is not None else ""

    raw_preview = raw[:300]

    text = (
        f"⚠️ <b>SINYAL AMBIGU — Butuh Konfirmasi Manual</b>\n\n"
        f"<blockquote>{raw_preview}</blockquote>"
        f"{confidence_str}"
        f"{reasons_block}\n\n"
        f"<i>⏰ Auto-abaikan dalam {settings.CONFIRMATION_TIMEOUT_MINUTES} menit jika tidak ada respons</i>"
    )

    sent = await bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode=ParseMode.HTML,
        reply_markup=_keyboard(key),
    )
    tg_msg_id = sent.message_id

    async def on_timeout(k: str, _payload: dict) -> None:
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=tg_msg_id,
                text=(
                    f"⏰ <b>Konfirmasi kedaluwarsa</b> — sinyal diabaikan otomatis.\n\n"
                    f"<blockquote>{raw[:200]}</blockquote>"
                ),
                parse_mode=ParseMode.HTML,
            )
        except Exception as exc:
            logger.warning("[signal_confirm] timeout edit error: %s", exc)

    pending_store.add(
        key,
        payload={
            "tg_msg_id":        tg_msg_id,
            "chat_id":          chat_id,
            "evaluation":       evaluation,
            "signal_message_id": signal_message_id,
        },
        timeout_seconds=timeout_sec,
        on_timeout=on_timeout,
    )
    logger.info("[signal_confirm] pending key=%s timeout=%ds", key, timeout_sec)


async def handle_signal_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """CallbackQueryHandler untuk prefix 'sig:'."""
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

    evaluation = payload["evaluation"]
    raw_preview = (getattr(evaluation, "raw_text", "") or "")[:200]

    if action == "skip":
        await query.edit_message_text(
            f"🚫 <b>Sinyal diabaikan.</b>\n\n<blockquote>{raw_preview}</blockquote>",
            parse_mode=ParseMode.HTML,
        )
        logger.info("[signal_confirm] key=%s → diabaikan oleh user", key)
        return

    if action == "edit":
        reasons = getattr(evaluation, "ambiguous_reasons", []) or []
        bullet  = "\n".join(f"  • {r}" for r in reasons) if reasons else "  (tidak ada detail)"
        await query.edit_message_text(
            f"✏️ <b>Field yang tidak pasti:</b>\n\n{bullet}\n\n"
            f"Kirim ulang sinyal yang sudah diperbaiki ke grup sinyal, atau gunakan command:\n"
            f"<code>/settp</code> / <code>/setsl</code> / <code>/close</code> untuk manajemen posisi manual.",
            parse_mode=ParseMode.HTML,
        )
        logger.info("[signal_confirm] key=%s → 'edit dulu' dipilih", key)
        return

    # action == "exec"
    if _execute_fn is None:
        await query.edit_message_text(
            "⚠️ Execute-fn belum terdaftar (akan di-wire di step 19). "
            "Eksekusi manual tidak tersedia.",
            parse_mode=ParseMode.HTML,
        )
        return

    await query.edit_message_text("⏳ Mengeksekusi sinyal...", parse_mode=ParseMode.HTML)
    try:
        result_text = await _execute_fn(evaluation)
    except Exception as exc:
        logger.exception("[signal_confirm] execute error: %s", exc)
        result_text = f"❌ Error eksekusi: {exc}"

    try:
        await query.edit_message_text(result_text, parse_mode=ParseMode.HTML)
    except Exception:
        pass
