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
from db.crud.trades import get_closed_trades, get_open_trades, get_open_trades_summary, get_daily_stats
from db.crud.circuit_breaker import get_all_cb_states
from db.database import check_db_health

logger = logging.getLogger(__name__)


async def _fetch_balance():
    """Ambil balance dari exchange. Return None jika gagal (tidak block command)."""
    try:
        from exchange.bitget.rest_client import BitgetRestClient
        async with BitgetRestClient() as client:
            return await client.fetch_balance()
    except Exception as exc:
        logger.warning(f"fetch_balance gagal di /dashboard: {exc}")
        return None


async def _send(update: Update, text: str) -> None:
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


@authorized
async def cmd_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    balance = await _fetch_balance()
    summary = get_open_trades_summary()
    daily = get_daily_stats()
    paused = is_bot_paused()
    await _send(update, fmt_dashboard(balance, summary, daily, paused))


@authorized
async def cmd_positions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    trades = get_open_trades()
    await _send(update, fmt_positions(trades))


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
