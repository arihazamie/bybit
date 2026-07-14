"""
bot/control_bot/commands/position.py
======================================
Step 17 — handler command manajemen posisi.
Step 18 — migrated ke TTL-based pending_store (timeout + on_timeout callback).

Perubahan step 18:
- _PENDING dict diganti dengan pending_store (TTL-aware, asyncio-based)
- Setiap konfirmasi dapat on_timeout → edit pesan lama jadi notif "kedaluwarsa"
- _store_pending / _pop_pending dihapus, diganti pending_store.add / .pop
"""

from __future__ import annotations

import asyncio
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from bot.control_bot.auth import authorized
from bot.control_bot.inline.pending_store import make_pending_key, pending_store
from bot.circuit_breaker.manager import get_circuit_breaker
from bot.executor.order_manager import (
    amend_entry_price,
    cancel_pending_order,
    close_all_positions,
    close_position,
    set_stop_loss,
    set_take_profit,
)
from core.constants import CloseReason, Direction
from core.logging_setup import get_logger
from config.settings import settings
from db.crud.settings import is_bot_paused, set_bot_paused
from db.crud.trades import (
    async_get_open_trade_for_pair,
    async_get_open_trades,
    async_get_filled_open_trade_for_pair,
    async_get_filled_open_trades,
    async_get_pending_trade_for_pair,
    async_get_pending_trades,
)

logger = get_logger(__name__)


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _reconcile_before_action() -> bool:
    """
    Cross-check DB open/pending trades vs posisi & order LIVE di exchange
    SEBELUM command ini melihat/mengubah sebuah trade. Tanpa ini, /settp,
    /setsl, /close, dll bisa beroperasi di atas trade yang sebenarnya sudah
    ditutup manual di exchange tapi belum ke-sync ke DB (gap WS/reconciliation)
    — hasilnya membingungkan (order gagal aneh) atau, lebih parah, keputusan
    diambil berdasarkan data posisi yang sudah tidak ada.

    Dibungkus timeout supaya command TIDAK PERNAH hang menunggu exchange.
    Kalau reconcile lambat/gagal, return False — caller WAJIB hentikan aksi
    dan kabari user, bukan diam-diam lanjut pakai data DB basi.
    """
    try:
        from bot.executor.order_sync import reconcile_on_startup
        await asyncio.wait_for(reconcile_on_startup(), timeout=8.0)
        return True
    except asyncio.TimeoutError:
        logger.warning("[position] Live reconciliation timeout (>8s) sebelum aksi — aksi dihentikan.")
        return False
    except Exception as exc:  # noqa: BLE001 — aksi dihentikan, jangan fail-open
        logger.warning(f"[position] Live reconciliation gagal sebelum aksi: {exc}")
        return False


_RECONCILE_FAIL_MSG = "❌ Gagal verifikasi ke exchange, coba lagi."


async def _send(update: Update, text: str, reply_markup=None):
    # effective_message tetap valid baik dipanggil dari command message
    # maupun dari tombol menu (callback_query) — Step 1.
    return await update.effective_message.reply_text(
        text, parse_mode=ParseMode.HTML, reply_markup=reply_markup
    )


def _validate_tp_side(direction: str, entry: Optional[float], price: float) -> Optional[str]:
    """
    Cek TP berada di sisi yang benar relatif ke entry berdasarkan arah posisi.
    LONG  -> TP wajib DI ATAS entry (profit saat harga naik).
    SHORT -> TP wajib DI BAWAH entry (profit saat harga turun).
    Return pesan error jika invalid, None jika valid (atau entry tidak diketahui).
    """
    if entry is None:
        return None
    d = (direction or "").lower()
    if d == Direction.LONG and price <= entry:
        return (
            f"❌ TP tidak valid untuk posisi LONG.\n"
            f"TP (<code>{price:g}</code>) harus <b>di atas</b> entry (<code>{entry:g}</code>)."
        )
    if d == Direction.SHORT and price >= entry:
        return (
            f"❌ TP tidak valid untuk posisi SHORT.\n"
            f"TP (<code>{price:g}</code>) harus <b>di bawah</b> entry (<code>{entry:g}</code>)."
        )
    return None


def _validate_sl_side(direction: str, entry: Optional[float], price: float) -> Optional[str]:
    """
    Cek SL berada di sisi yang benar relatif ke entry berdasarkan arah posisi.
    LONG  -> SL wajib DI BAWAH entry.
    SHORT -> SL wajib DI ATAS entry.
    """
    if entry is None:
        return None
    d = (direction or "").lower()
    if d == Direction.LONG and price >= entry:
        return (
            f"❌ SL tidak valid untuk posisi LONG.\n"
            f"SL (<code>{price:g}</code>) harus <b>di bawah</b> entry (<code>{entry:g}</code>)."
        )
    if d == Direction.SHORT and price <= entry:
        return (
            f"❌ SL tidak valid untuk posisi SHORT.\n"
            f"SL (<code>{price:g}</code>) harus <b>di atas</b> entry (<code>{entry:g}</code>)."
        )
    return None


def _confirm_kb(key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Konfirmasi", callback_data=f"pos:{key}:y"),
        InlineKeyboardButton("❌ Batal",      callback_data=f"pos:{key}:n"),
    ]])


async def _send_confirm_with_ttl(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    action: str,
    **payload_kwargs,
) -> str:
    """
    Kirim pesan konfirmasi, simpan ke pending_store dengan TTL.
    Timeout → edit pesan jadi notif kedaluwarsa.
    Kembalikan key.
    """
    key = make_pending_key()
    timeout_sec = settings.CONFIRMATION_TIMEOUT_MINUTES * 60
    chat_id = update.effective_chat.id

    sent = await _send(update, text, reply_markup=_confirm_kb(key))
    tg_msg_id = sent.message_id

    async def on_timeout(k: str, _p: dict) -> None:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=tg_msg_id,
                text="⏰ <b>Konfirmasi kedaluwarsa</b> — aksi dibatalkan otomatis.",
                parse_mode=ParseMode.HTML,
            )
        except Exception as exc:
            logger.warning("[position] timeout edit error key=%s: %s", k, exc)

    pending_store.add(
        key,
        payload={"action": action, "tg_msg_id": tg_msg_id, **payload_kwargs},
        timeout_seconds=timeout_sec,
        on_timeout=on_timeout,
    )
    return key


# ── /settp {pair} {harga} ─────────────────────────────────────────────────────

@authorized
async def cmd_settp(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args or []) < 2:
        await _send(update,
            "❌ Format: <code>/settp {pair} {harga}</code>\n"
            "Contoh: <code>/settp BTC/USDT:USDT 70000</code>")
        return

    pair = context.args[0].upper()
    try:
        price = float(context.args[1])
    except ValueError:
        await _send(update, "❌ Harga harus angka.")
        return
    if price <= 0:
        await _send(update, "❌ Harga harus lebih dari 0.")
        return

    if not await _reconcile_before_action():
        await _send(update, _RECONCILE_FAIL_MSG)
        return
    trade = await async_get_filled_open_trade_for_pair(pair)
    if not trade:
        pending = await async_get_pending_trade_for_pair(pair)
        if pending:
            await _send(
                update,
                f"❌ <code>{pair}</code> masih <b>PENDING</b> (order belum fill di exchange).\n"
                f"TP belum bisa dipasang live — belum ada posisi untuk dipasangi TP order.\n"
                f"Set TP lagi setelah order fill.",
            )
            return
        await _send(update, f"❌ Tidak ada posisi <b>OPEN</b> untuk <code>{pair}</code>.")
        return

    direction = (trade.get("direction") or "?").upper()
    entry     = trade.get("entry_price")
    entry_str = f"{entry:g}" if entry else "?"

    err = _validate_tp_side(trade.get("direction") or "", entry, price)
    if err:
        await _send(update, err)
        return

    await _send_confirm_with_ttl(
        update, context,
        f"⚠️ <b>Set Take Profit</b>\n\n"
        f"Pair     : <code>{pair}</code> {direction}\n"
        f"Entry    : <code>{entry_str}</code>\n"
        f"TP baru  : <code>{price:g}</code>\n"
        f"Trade    : #{trade['id']}\n\n"
        f"<i>TP order akan dipasang LIVE di exchange (TPSL, terikat ke posisi).</i>",
        action="settp",
        pair=pair, trade_id=trade["id"], price=price,
    )


# ── /setsl {pair} {harga} ─────────────────────────────────────────────────────

@authorized
async def cmd_setsl(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args or []) < 2:
        await _send(update,
            "❌ Format: <code>/setsl {pair} {harga}</code>\n"
            "Contoh: <code>/setsl BTC/USDT:USDT 65000</code>")
        return

    pair = context.args[0].upper()
    try:
        price = float(context.args[1])
    except ValueError:
        await _send(update, "❌ Harga harus angka.")
        return
    if price <= 0:
        await _send(update, "❌ Harga harus lebih dari 0.")
        return

    if not await _reconcile_before_action():
        await _send(update, _RECONCILE_FAIL_MSG)
        return
    trade = await async_get_filled_open_trade_for_pair(pair)
    if not trade:
        pending = await async_get_pending_trade_for_pair(pair)
        if pending:
            await _send(
                update,
                f"❌ <code>{pair}</code> masih <b>PENDING</b> (order belum fill di exchange).\n"
                f"SL belum bisa diset — belum ada posisi live untuk dipasangi stop order.\n"
                f"SL otomatis dipasang begitu order fill.",
            )
            return
        await _send(update, f"❌ Tidak ada posisi <b>OPEN</b> untuk <code>{pair}</code>.")
        return
    old_str = f"{old_sl:g}" if old_sl else "tidak ada"
    direction = (trade.get("direction") or "?").upper()
    entry     = trade.get("entry_price")

    err = _validate_sl_side(trade.get("direction") or "", entry, price)
    if err:
        await _send(update, err)
        return

    await _send_confirm_with_ttl(
        update, context,
        f"⚠️ <b>Update Stop Loss</b>\n\n"
        f"Pair    : <code>{pair}</code> {direction}\n"
        f"SL lama : <code>{old_str}</code>\n"
        f"SL baru : <code>{price:g}</code>\n"
        f"Trade   : #{trade['id']}\n\n"
        f"<i>SL order lama akan di-cancel dan SL baru dipasang di exchange.</i>",
        action="setsl",
        pair=pair, trade_id=trade["id"], price=price,
    )


# ── /setentry {pair} {harga} ──────────────────────────────────────────────────

@authorized
async def cmd_setentry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args or []) < 2:
        await _send(update,
            "❌ Format: <code>/setentry {pair} {harga}</code>\n"
            "Contoh: <code>/setentry BTC/USDT:USDT 68000</code>")
        return

    pair = context.args[0].upper()
    try:
        price = float(context.args[1])
    except ValueError:
        await _send(update, "❌ Harga harus angka.")
        return
    if price <= 0:
        await _send(update, "❌ Harga harus lebih dari 0.")
        return

    if not await _reconcile_before_action():
        await _send(update, _RECONCILE_FAIL_MSG)
        return
    trade = await async_get_pending_trade_for_pair(pair)
    if not trade:
        await _send(update, f"❌ Tidak ada order <b>PENDING</b> untuk <code>{pair}</code>.")
        return

    old_entry = trade.get("entry_price")
    old_str   = f"{old_entry:g}" if old_entry else "?"
    direction = (trade.get("direction") or "?").upper()
    await _send_confirm_with_ttl(
        update, context,
        f"⚠️ <b>Update Entry Price</b>\n\n"
        f"Pair       : <code>{pair}</code> {direction}\n"
        f"Entry lama : <code>{old_str}</code>\n"
        f"Entry baru : <code>{price:g}</code>\n"
        f"Trade      : #{trade['id']}\n\n"
        f"<i>Limit order lama akan di-cancel di exchange, lalu order baru\n"
        f"dipasang di harga baru (SL lama tetap dipasang ulang otomatis).</i>",
        action="setentry",
        pair=pair, trade_id=trade["id"], price=price,
    )


# ── /close {pair} ─────────────────────────────────────────────────────────────

@authorized
async def cmd_close(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await _send(update,
            "❌ Format: <code>/close {pair}</code>\n"
            "Contoh: <code>/close BTC/USDT:USDT</code>")
        return

    pair  = context.args[0].upper()
    if not await _reconcile_before_action():
        await _send(update, _RECONCILE_FAIL_MSG)
        return
    trade = await async_get_filled_open_trade_for_pair(pair)
    if not trade:
        pending = await async_get_pending_trade_for_pair(pair)
        if pending:
            await _send(
                update,
                f"❌ <code>{pair}</code> masih <b>PENDING</b>, belum ada posisi live di exchange.\n"
                f"Gunakan <code>/cancel {pair}</code> untuk batalkan order pending.",
            )
            return
        await _send(update, f"❌ Tidak ada posisi <b>OPEN</b> untuk <code>{pair}</code>.")
        return

    direction = (trade.get("direction") or "?").upper()
    entry_str = f"{trade['entry_price']:g}" if trade.get("entry_price") else "?"
    size_str  = f"{trade['position_size']:g}" if trade.get("position_size") else "?"
    sl_str    = f"{trade['sl_price']:g}" if trade.get("sl_price") else "tidak ada"
    await _send_confirm_with_ttl(
        update, context,
        f"⚠️ <b>Close Posisi</b>\n\n"
        f"Pair    : <code>{pair}</code>\n"
        f"Arah    : <b>{direction}</b>\n"
        f"Entry   : <code>{entry_str}</code>\n"
        f"Size    : <code>{size_str}</code>\n"
        f"SL      : <code>{sl_str}</code>\n"
        f"Trade   : #{trade['id']}\n\n"
        f"<i>Posisi akan di-close by market order. Tidak bisa di-undo.</i>",
        action="close",
        pair=pair, trade_id=trade["id"],
    )


# ── /closeall ─────────────────────────────────────────────────────────────────

@authorized
async def cmd_closeall(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _reconcile_before_action():
        await _send(update, _RECONCILE_FAIL_MSG)
        return
    open_trades = await async_get_filled_open_trades()
    if not open_trades:
        await _send(update, "ℹ️ Tidak ada posisi open saat ini.")
        return

    pairs_str = "\n".join(
        f"  • <code>{t['pair']}</code> {(t.get('direction') or '').upper()}"
        for t in open_trades
    )
    await _send_confirm_with_ttl(
        update, context,
        f"🚨 <b>Close Semua Posisi</b>\n\n"
        f"Posisi yang akan di-close ({len(open_trades)}):\n{pairs_str}\n\n"
        f"<i>Semua posisi di-close by market order. Tidak bisa di-undo.</i>",
        action="closeall",
    )


# ── /pending ──────────────────────────────────────────────────────────────────

@authorized
async def cmd_pending(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _reconcile_before_action():
        await _send(update, _RECONCILE_FAIL_MSG)
        return
    pending = await async_get_pending_trades()
    if not pending:
        await _send(update, "ℹ️ Tidak ada pending order saat ini.")
        return

    lines = [f"<b>⏳ Pending Orders ({len(pending)})</b>\n"]
    for t in pending:
        entry     = t.get("entry_price")
        sl        = t.get("sl_price")
        direction = (t.get("direction") or "?").upper()
        entry_str = f"{entry:g}" if entry else "?"
        sl_str    = f"{sl:g}"    if sl    else "?"
        lines.append(
            f"• <code>{t['pair']}</code> <b>{direction}</b>\n"
            f"  Entry: <code>{entry_str}</code> | SL: <code>{sl_str}</code>"
            f" | #<code>{t['id']}</code>\n"
            f"  Cancel: <code>/cancel {t['pair']}</code>"
        )
    await _send(update, "\n\n".join(lines))


# ── /cancel {pair} ────────────────────────────────────────────────────────────

@authorized
async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await _send(update,
            "❌ Format: <code>/cancel {pair}</code>\n"
            "Contoh: <code>/cancel BTC/USDT:USDT</code>")
        return

    pair  = context.args[0].upper()
    if not await _reconcile_before_action():
        await _send(update, _RECONCILE_FAIL_MSG)
        return
    trade = await async_get_pending_trade_for_pair(pair)
    if not trade:
        await _send(update, f"❌ Tidak ada order <b>PENDING</b> untuk <code>{pair}</code>.")
        return

    entry_str = f"{trade['entry_price']:g}" if trade.get("entry_price") else "?"
    direction = (trade.get("direction") or "?").upper()
    await _send_confirm_with_ttl(
        update, context,
        f"⚠️ <b>Cancel Pending Order</b>\n\n"
        f"Pair  : <code>{pair}</code>\n"
        f"Arah  : <b>{direction}</b>\n"
        f"Entry : <code>{entry_str}</code>\n"
        f"Trade : #{trade['id']}",
        action="cancel",
        pair=pair, trade_id=trade["id"],
    )


# ── /pause ────────────────────────────────────────────────────────────────────

@authorized
async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if is_bot_paused():
        await _send(update,
            "ℹ️ Bot sudah dalam mode <b>PAUSE</b>.\n"
            "Gunakan <code>/resume</code> untuk melanjutkan.")
        return
    set_bot_paused(True)
    logger.info("Bot di-pause via /pause command")
    await _send(
        update,
        "⏸ <b>Bot di-PAUSE</b>\n\n"
        "Sinyal baru tidak akan dieksekusi.\n"
        "Posisi yang sudah open tetap dipantau.\n\n"
        "Aktifkan kembali: <code>/resume</code>"
    )


# ── /resume ───────────────────────────────────────────────────────────────────

@authorized
async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /resume harus mengembalikan bot ke kondisi bisa eksekusi sinyal lagi
    secara PENUH — bukan cuma un-pause. Kalau circuit breaker sedang OPEN
    (mis. abis trip gara-gara error beruntun di order_execution), eksekusi
    TETAP diblokir oleh CB walau bot_paused sudah False. Jadi /resume di sini
    juga wajib panggil cb.resume() (OPEN → HALF_OPEN) — sebelumnya command
    ini cuma toggle is_bot_paused dan tidak pernah menyentuh circuit breaker
    sama sekali, jadi user yang CB-nya OPEN tetap stuck walau sudah /resume.
    """
    was_paused = is_bot_paused()
    if was_paused:
        set_bot_paused(False)
        logger.info("Bot di-resume via /resume command")

    cb = get_circuit_breaker()
    transitioned = await cb.resume()

    lines = []
    if was_paused:
        lines.append("▶️ <b>Bot AKTIF kembali</b> (keluar dari mode PAUSE).")
    else:
        lines.append("ℹ️ Bot sudah AKTIF (tidak dalam mode pause).")

    if transitioned:
        comp_list = ", ".join(f"<code>{c}</code>" for c in transitioned)
        lines.append(
            f"\n🟡 Circuit breaker di-resume ke <b>HALF_OPEN</b>: {comp_list}\n"
            "Eksekusi berikutnya jadi probe — kalau sukses otomatis balik CLOSED, "
            "kalau gagal balik OPEN lagi (dan /resume harus dikirim ulang)."
        )
    elif not was_paused:
        lines.append("\nTidak ada circuit breaker yang sedang OPEN.")

    lines.append("\nCek status: <code>/status</code> | <code>/settings</code>")
    await _send(update, "\n".join(lines))


# ── Callback query handler ────────────────────────────────────────────────────

async def handle_position_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handler inline button konfirmasi posisi (callback_data prefix 'pos:')."""
    query = update.callback_query
    await query.answer()

    data  = query.data or ""
    if not data.startswith("pos:"):
        return

    parts = data.split(":")
    if len(parts) != 3:
        return

    _, key, choice = parts
    payload = pending_store.pop(key)

    if payload is None:
        await query.edit_message_text(
            "⚠️ Konfirmasi sudah kedaluwarsa atau sudah diproses.",
            parse_mode=ParseMode.HTML,
        )
        return

    if choice == "n":
        await query.edit_message_text("❌ <b>Dibatalkan.</b>", parse_mode=ParseMode.HTML)
        return

    # choice == "y"
    action = payload["action"]
    try:
        if action == "settp":
            result_text = await _exec_settp(payload)
        elif action == "setsl":
            result_text = await _exec_setsl(payload)
        elif action == "setentry":
            result_text = await _exec_setentry(payload)
        elif action == "close":
            result_text = await _exec_close(payload)
        elif action == "closeall":
            result_text = await _exec_closeall(payload)
        elif action == "cancel":
            result_text = await _exec_cancel(payload)
        else:
            result_text = f"❌ Aksi tidak dikenali: <code>{action}</code>"
    except Exception as exc:
        logger.exception("[position_callback] Unexpected error action=%s: %s", action, exc)
        result_text = f"❌ Error tidak terduga: {exc}"

    await query.edit_message_text(result_text, parse_mode=ParseMode.HTML)


# ── Executor helpers ──────────────────────────────────────────────────────────

async def _exec_settp(p: dict) -> str:
    pair, trade_id, price = p["pair"], p["trade_id"], p["price"]
    result = await set_take_profit(trade_id, price)
    if result.success:
        dry_tag = "🔵 [DRY-RUN] " if result.is_dry_run else ""
        note = f"\n{result.notes[0]}" if result.notes else ""
        return (
            f"{dry_tag}✅ <b>Take Profit dipasang di exchange</b>\n\n"
            f"Pair  : <code>{pair}</code>\n"
            f"TP    : <code>{price:g}</code>\n"
            f"Trade : #{trade_id}{note}"
        )
    crit = "🔴 CRITICAL — " if result.is_critical else ""
    return f"❌ {crit}Gagal set TP: {result.failure_reason}"


async def _exec_setsl(p: dict) -> str:
    pair, trade_id, price = p["pair"], p["trade_id"], p["price"]
    result = await set_stop_loss(trade_id, price)
    if result.success:
        dry_tag = "🔵 [DRY-RUN] " if result.is_dry_run else ""
        sl_id   = f"\nSL order ID : <code>{result.sl_order_id}</code>" if result.sl_order_id else ""
        return (
            f"{dry_tag}✅ <b>Stop Loss diupdate</b>\n\n"
            f"Pair    : <code>{pair}</code>\n"
            f"SL baru : <code>{price:g}</code>\n"
            f"Trade   : #{trade_id}{sl_id}"
        )
    crit = "🔴 CRITICAL — " if result.is_critical else ""
    return f"❌ {crit}Gagal update SL: {result.failure_reason}"


async def _exec_setentry(p: dict) -> str:
    pair, trade_id, price = p["pair"], p["trade_id"], p["price"]
    result = await amend_entry_price(trade_id, price)
    if result.success:
        dry_tag = "🔵 [DRY-RUN] " if result.is_dry_run else ""
        note = f"\n{result.notes[0]}" if result.notes else ""
        return (
            f"{dry_tag}✅ <b>Entry price diamend di exchange</b>\n\n"
            f"Pair       : <code>{pair}</code>\n"
            f"Entry baru : <code>{price:g}</code>\n"
            f"Trade      : #{trade_id}{note}"
        )
    crit = "🔴 CRITICAL — " if result.is_critical else ""
    return f"❌ {crit}Gagal amend entry: {result.failure_reason}"


async def _exec_close(p: dict) -> str:
    pair, trade_id = p["pair"], p["trade_id"]
    result = await close_position(trade_id, close_reason=CloseReason.MANUAL)
    if result.success:
        dry_tag = "🔵 [DRY-RUN] " if result.is_dry_run else ""
        pnl_str = f"{result.closed_pnl:+.4f} USDT" if result.closed_pnl is not None else "N/A"
        return (
            f"{dry_tag}✅ <b>Posisi closed</b>\n\n"
            f"Pair     : <code>{pair}</code>\n"
            f"PnL est. : <code>{pnl_str}</code>\n"
            f"Trade    : #{trade_id}"
        )
    crit = "🔴 CRITICAL — " if result.is_critical else ""
    return f"❌ {crit}Gagal close posisi: {result.failure_reason}"


async def _exec_closeall(_p: dict) -> str:
    result = await close_all_positions(close_reason=CloseReason.MANUAL)
    return result.notification_text()


async def _exec_cancel(p: dict) -> str:
    pair, trade_id = p["pair"], p["trade_id"]
    result = await cancel_pending_order(trade_id)
    if result.success:
        dry_tag = "🔵 [DRY-RUN] " if result.is_dry_run else ""
        return (
            f"{dry_tag}✅ <b>Order dibatalkan</b>\n\n"
            f"Pair  : <code>{pair}</code>\n"
            f"Trade : #{trade_id}"
        )
    crit = "🔴 CRITICAL — " if result.is_critical else ""
    return f"❌ {crit}Gagal cancel order: {result.failure_reason}"