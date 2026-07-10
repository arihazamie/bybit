"""
bot/control_bot/commands/risk.py
==================================
Step 16 — handler command risk & leverage:

  /setrisk {persen}          → aktifkan mode Percent, set risk_percent
  /setmaxloss {nominal}      → aktifkan mode Fixed USD, set max_loss_usd
  /riskmode                  → tampilkan mode & nilai aktif
  /setleverage {pair} {cap}  → set cap leverage manual untuk pair (0 = hapus cap)
  /leverage {pair}           → cek max leverage dari exchange + cap aktif
  /conflictmode {mode}       → set position conflict mode (ask/skip/add/replace)
"""

from __future__ import annotations

import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from bot.control_bot.auth import authorized
from db.crud.settings import (
    get_all_leverage_caps,
    get_leverage_cap,
    get_max_loss_usd,
    get_position_conflict_mode,
    get_risk_mode,
    get_risk_percent,
    set_leverage_cap,
    set_setting,
)

logger = logging.getLogger(__name__)

_CONFLICT_MODES = {"ask", "skip", "add", "replace"}


async def _send(update: Update, text: str) -> None:
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


# ── /setrisk {persen} ─────────────────────────────────────────────────────────

@authorized
async def cmd_setrisk(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await _send(update, "❌ Format: <code>/setrisk {persen}</code>\nContoh: <code>/setrisk 1.5</code>")
        return

    try:
        pct = float(context.args[0])
    except ValueError:
        await _send(update, "❌ Nilai harus angka. Contoh: <code>/setrisk 1.5</code>")
        return

    if pct <= 0 or pct > 100:
        await _send(update, "❌ Persen harus antara 0.01 dan 100.")
        return

    set_setting("risk_mode", "percent")
    set_setting("risk_percent", str(pct))
    logger.info(f"Risk mode → percent, {pct}%")

    await _send(
        update,
        f"✅ Risk mode diubah ke <b>Percent</b>\n"
        f"Max loss per trade: <code>{pct}%</code> dari total balance\n\n"
        f"<i>Mode Fixed USD dinonaktifkan.</i>"
    )


# ── /setmaxloss {nominal} ─────────────────────────────────────────────────────

@authorized
async def cmd_setmaxloss(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await _send(update, "❌ Format: <code>/setmaxloss {nominal}</code>\nContoh: <code>/setmaxloss 5</code>")
        return

    try:
        usd = float(context.args[0])
    except ValueError:
        await _send(update, "❌ Nilai harus angka. Contoh: <code>/setmaxloss 5</code>")
        return

    if usd <= 0:
        await _send(update, "❌ Nilai harus lebih dari 0.")
        return

    set_setting("risk_mode", "fixed_usd")
    set_setting("max_loss_usd", str(usd))
    logger.info(f"Risk mode → fixed_usd, ${usd}")

    await _send(
        update,
        f"✅ Risk mode diubah ke <b>Fixed USD</b>\n"
        f"Max loss per trade: <code>${usd:.2f}</code> (tetap, tidak bergantung balance)\n\n"
        f"<i>Mode Percent dinonaktifkan.</i>"
    )


# ── /riskmode ─────────────────────────────────────────────────────────────────

@authorized
async def cmd_riskmode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    mode = get_risk_mode()
    pct = get_risk_percent()
    usd = get_max_loss_usd()

    if mode == "percent":
        active_line = f"<b>Percent</b> — <code>{pct}%</code> dari total balance per trade"
        inactive_line = f"Fixed USD ($<code>{usd:.2f}</code>) — <i>tidak aktif</i>"
    else:
        active_line = f"<b>Fixed USD</b> — <code>${usd:.2f}</code> per trade (tetap)"
        inactive_line = f"Percent (<code>{pct}%</code>) — <i>tidak aktif</i>"

    text = (
        f"<b>⚖️ Risk Mode Aktif</b>\n\n"
        f"✅ {active_line}\n"
        f"○  {inactive_line}\n\n"
        f"Ubah: <code>/setrisk {pct}</code> atau <code>/setmaxloss {usd:.2f}</code>"
    )
    await _send(update, text)


# ── /setleverage {pair} {cap} ─────────────────────────────────────────────────

@authorized
async def cmd_setleverage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args or len(context.args) < 2:
        await _send(
            update,
            "❌ Format: <code>/setleverage {pair} {cap}</code>\n"
            "Contoh: <code>/setleverage BTC/USDT:USDT 50</code>\n"
            "Set cap=0 untuk hapus cap dan pakai max dari exchange."
        )
        return

    pair = context.args[0].upper()
    try:
        cap = float(context.args[1])
    except ValueError:
        await _send(update, "❌ Cap harus angka. Contoh: <code>/setleverage BTC/USDT:USDT 50</code>")
        return

    if cap < 0:
        await _send(update, "❌ Cap tidak boleh negatif. Gunakan 0 untuk hapus cap.")
        return

    set_leverage_cap(pair, cap)
    logger.info(f"Leverage cap set: {pair} → {cap}x")

    if cap == 0:
        await _send(
            update,
            f"✅ Cap leverage untuk <code>{pair}</code> <b>dihapus</b>\n"
            f"Bot akan pakai max leverage dari exchange untuk pair ini."
        )
    else:
        await _send(
            update,
            f"✅ Cap leverage untuk <code>{pair}</code> diset ke <b>{cap:.0f}x</b>\n"
            f"Bot tidak akan melebihi {cap:.0f}x untuk pair ini, meski exchange izinkan lebih tinggi."
        )


# ── /leverage {pair} ──────────────────────────────────────────────────────────

@authorized
async def cmd_leverage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        # Tampilkan semua cap yang aktif
        caps = get_all_leverage_caps()
        if not caps:
            await _send(
                update,
                "ℹ️ Tidak ada leverage cap yang diset.\n"
                "Bot pakai max leverage dari exchange untuk semua pair.\n\n"
                "Cek leverage pair tertentu: <code>/leverage BTC/USDT:USDT</code>"
            )
            return

        lines = ["<b>⚙️ Leverage Cap Aktif</b>\n"]
        for p, c in sorted(caps.items()):
            label = "global" if p == "_global" else p
            lines.append(f"  <code>{label:<25}</code> → <b>{c:.0f}x</b>")
        lines.append("\nHapus cap: <code>/setleverage {pair} 0</code>")
        await _send(update, "\n".join(lines))
        return

    pair = context.args[0].upper()

    # Coba fetch max leverage dari exchange
    max_lev: float | None = None
    fetch_error: str | None = None
    try:
        from exchange.bitget.rest_client import BitgetRestClient
        async with BitgetRestClient() as client:
            max_lev = await client.get_max_leverage(pair)
    except Exception as exc:
        fetch_error = str(exc)
        logger.warning(f"/leverage exchange fetch gagal untuk {pair}: {exc}")

    cap = get_leverage_cap(pair)
    from db.crud.settings import get_setting
    liq_buf = float(get_setting("liquidation_buffer_pct") or "5.0")

    lines = [f"<b>🔧 Leverage Info — <code>{pair}</code></b>\n"]

    if max_lev:
        lines.append(f"Max dari exchange : <b>{max_lev:.0f}x</b>")
    else:
        lines.append(f"Max dari exchange : <i>gagal fetch — {fetch_error or 'unknown'}</i>")

    if cap:
        effective = min(cap, max_lev) if max_lev else cap
        lines.append(f"Cap manual        : <b>{cap:.0f}x</b>")
        lines.append(f"Leverage efektif  : <b>{effective:.0f}x</b>")
    else:
        effective = max_lev
        lines.append(f"Cap manual        : tidak ada (pakai max exchange)")
        if max_lev:
            lines.append(f"Leverage efektif  : <b>{max_lev:.0f}x</b>")

    lines.append(f"\nLiquidation buffer: <code>{liq_buf:.1f}%</code>")
    lines.append(
        "<i>Bot akan turunkan leverage otomatis jika estimasi liquidation\n"
        "lebih dekat dari buffer ini terhadap SL.</i>"
    )

    if cap:
        lines.append(f"\nUbah cap: <code>/setleverage {pair} {cap:.0f}</code>")
        lines.append(f"Hapus cap: <code>/setleverage {pair} 0</code>")
    else:
        lines.append(f"\nSet cap: <code>/setleverage {pair} 50</code>")

    await _send(update, "\n".join(lines))


# ── /conflictmode {mode} ──────────────────────────────────────────────────────

@authorized
async def cmd_conflictmode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        current = get_position_conflict_mode()
        text = (
            f"<b>⚔️ Position Conflict Mode</b>\n\n"
            f"Mode aktif: <b>{current}</b>\n\n"
            f"<b>ask</b>    — tanya via inline button (default)\n"
            f"<b>skip</b>   — abaikan sinyal baru otomatis\n"
            f"<b>add</b>    — buka posisi tambahan tanpa konfirmasi\n"
            f"<b>replace</b> — cancel/close posisi lama, buka yang baru\n\n"
            f"Ubah: <code>/conflictmode ask</code>"
        )
        await _send(update, text)
        return

    mode = context.args[0].lower()
    if mode not in _CONFLICT_MODES:
        await _send(
            update,
            f"❌ Mode tidak valid: <code>{mode}</code>\n"
            f"Pilihan: <code>ask</code>, <code>skip</code>, <code>add</code>, <code>replace</code>"
        )
        return

    set_setting("position_conflict_mode", mode)
    logger.info(f"Conflict mode → {mode}")

    descriptions = {
        "ask":     "Bot akan mengirim inline button untuk konfirmasi setiap kali ada konflik posisi.",
        "skip":    "Sinyal baru diabaikan otomatis jika sudah ada posisi/order untuk pair yang sama.",
        "add":     "Bot langsung buka posisi tambahan tanpa konfirmasi.",
        "replace": "Bot cancel/close posisi lama dan buka yang baru tanpa konfirmasi.",
    }

    await _send(
        update,
        f"✅ Conflict mode diubah ke <b>{mode}</b>\n\n"
        f"{descriptions[mode]}"
    )
