"""
bot/control_bot/commands/info.py
=================================
Handler info commands (read-only):
  /dashboard  — posisi open, balance, P&L hari ini
  /positions  — detail tiap posisi open/pending
  /history    — N trade terakhir yang sudah ditutup (default 10)
  /settings   — semua setting aktif
  /status     — health circuit breaker + DB
"""

from __future__ import annotations

import asyncio
import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from bot.control_bot.auth import authorized
from bot.control_bot.formatters import (
    fmt_dashboard,
    fmt_history,
    fmt_positions,
    fmt_settings,
    fmt_status,
)
from db.crud.settings import get_all_settings, is_bot_paused
from db.crud.trades import (
    async_get_open_trades,
    async_get_open_trades_summary,
    get_closed_trades,
    get_daily_stats,
)
from db.crud.circuit_breaker import get_all_cb_states
from db.database import check_db_health

logger = logging.getLogger(__name__)


async def _reconcile_before_read() -> bool:
    """
    Cross-check DB open/pending trades vs posisi & order LIVE di exchange
    sebelum menampilkan /positions atau /dashboard. Tanpa ini, kalau WS
    sempat putus atau reconciliation loop belum sempat jalan (interval
    default 60 detik), command ini bisa menampilkan posisi "open" yang
    sebenarnya sudah ditutup manual di exchange — laporan jadi menyesatkan
    dan berbahaya untuk keputusan trading berikutnya.

    Memakai jalur yang sama dengan startup reconciliation (order_sync.py)
    supaya close/cancel yang terdeteksi di sini juga tercatat & dinotifikasi
    dengan cara yang konsisten (close_reason, PnL, dst), bukan cuma "hilang
    diam-diam" dari laporan.

    Dibungkus timeout supaya /positions dan /dashboard TIDAK PERNAH hang
    menunggu exchange. Kalau reconcile lambat/gagal, return False — caller
    WAJIB hentikan laporan dan kabari user, bukan diam-diam tampilkan data
    DB basi.
    """
    try:
        from bot.executor.order_sync import reconcile_on_startup
        await asyncio.wait_for(reconcile_on_startup(), timeout=8.0)
        return True
    except asyncio.TimeoutError:
        logger.warning("Live reconciliation timeout (>8s) sebelum /positions atau /dashboard.")
        return False
    except Exception as exc:  # noqa: BLE001 — laporan dihentikan, jangan fail-open
        logger.warning(f"Live reconciliation gagal sebelum /positions atau /dashboard: {exc}")
        return False


_RECONCILE_FAIL_MSG = "❌ Gagal verifikasi ke exchange, coba lagi."


async def _fetch_balance():
    """Ambil balance dari exchange. Return None jika gagal (tidak block command)."""
    try:
        from exchange.bitget.rest_client import BitgetRestClient
        async with BitgetRestClient() as client:
            return await client.fetch_balance()
    except Exception as exc:
        logger.warning(f"fetch_balance gagal di /dashboard: {exc}")
        return None


async def _fetch_live_positions() -> dict:
    """
    Ambil posisi live (REST) dari exchange untuk keperluan /positions —
    dipetakan {symbol: raw ccxt position dict} supaya formatter bisa
    menampilkan harga sekarang, floating P/L ($), dan P/L (%) yang akurat
    langsung dari Bitget, bukan sekadar data statis dari DB.

    Return dict kosong (bukan raise) kalau gagal — /positions tetap harus
    tampil walau exchange sedang tidak bisa diakses, hanya tanpa data live.
    """
    try:
        from exchange.bitget.rest_client import BitgetRestClient
        async with BitgetRestClient() as client:
            positions = await client.fetch_positions()
        return {
            p["symbol"]: p
            for p in positions
            if p.get("symbol") and (p.get("contracts") or 0) != 0
        }
    except Exception as exc:
        logger.warning(f"fetch_positions gagal di /positions: {exc}")
        return {}


async def _send(update: Update, text: str) -> None:
    # effective_message tetap valid baik dipanggil dari command message
    # (/dashboard, dst) maupun dari tombol menu (callback_query) — Step 1.
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)


@authorized
async def cmd_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _reconcile_before_read():
        await _send(update, _RECONCILE_FAIL_MSG)
        return
    balance = await _fetch_balance()
    summary = await async_get_open_trades_summary()
    daily = get_daily_stats()
    paused = is_bot_paused()
    await _send(update, fmt_dashboard(balance, summary, daily, paused))


@authorized
async def cmd_positions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _reconcile_before_read():
        await _send(update, _RECONCILE_FAIL_MSG)
        return
    trades = await async_get_open_trades()
    live_positions = await _fetch_live_positions()
    await _send(update, fmt_positions(trades, live_positions))


@authorized
async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    n = 10
    if context.args:
        try:
            n = max(1, min(int(context.args[0]), 50))
        except ValueError:
            await _send(update, "❌ Format: <code>/history [N]</code> — N harus angka (maks 50).")
            return

    trades = get_closed_trades(limit=n)
    await _send(update, fmt_history(trades))


@authorized
async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    all_s = get_all_settings()
    await _send(update, fmt_settings(all_s))


@authorized
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cb_states = get_all_cb_states()
    db_health = check_db_health()
    paused = is_bot_paused()
    await _send(update, fmt_status(cb_states, db_health, paused))