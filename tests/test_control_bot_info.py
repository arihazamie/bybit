"""
tests/test_control_bot_info.py
================================
Unit tests untuk Step 15 — formatters & handler logic.
Tidak ada network call ke Telegram (semua di-mock).
"""

import pytest

from bot.control_bot.formatters import (
    fmt_dashboard,
    fmt_history,
    fmt_positions,
    fmt_settings,
    fmt_status,
)


# ── fmt_dashboard ────────────────────────────────────────────────────────────

def test_dashboard_no_balance():
    summary = {"open_count": 0, "pending_count": 0, "total_margin_used": 0, "pairs_open": []}
    daily = {"date": "2025-01-01", "total_trades": 0, "winning_trades": 0, "total_pnl": 0}
    text = fmt_dashboard(None, summary, daily, is_paused=True)
    assert "Dashboard" in text
    assert "tidak tersedia" in text
    assert "PAUSED" in text


def test_dashboard_with_balance():
    class FakeBalance:
        total_equity = 1000.0
        free_margin = 800.0
        wallet_balance = 950.0
        unrealized_pnl = 50.0

    summary = {"open_count": 2, "pending_count": 1, "total_margin_used": 45.5, "pairs_open": ["BTC/USDT:USDT"]}
    daily = {"date": "2025-01-01", "total_trades": 3, "winning_trades": 2, "total_pnl": 12.5}
    text = fmt_dashboard(FakeBalance(), summary, daily, is_paused=False)
    assert "1,000.00 USDT" in text
    assert "RUNNING" in text
    assert "BTC/USDT:USDT" in text


# ── fmt_positions ─────────────────────────────────────────────────────────────

def test_positions_empty():
    text = fmt_positions([])
    assert "Tidak ada" in text


def test_positions_format():
    trades = [
        {
            "pair": "ETH/USDT:USDT",
            "direction": "long",
            "status": "open",
            "entry_price": 3200.0,
            "sl_price": 3100.0,
            "tp_price": None,
            "position_size": 0.1,
            "leverage_used": 20.0,
            "margin_used": 16.0,
            "risk_amount_usd": 10.0,
            "risk_mode": "percent",
            "leverage_auto_adjusted": False,
            "opened_at": "2025-01-01T10:00:00",
            "created_at": "2025-01-01T09:59:00",
        }
    ]
    text = fmt_positions(trades)
    assert "ETH/USDT:USDT" in text
    assert "LONG" in text
    assert "3200" in text
    assert "20x" in text


def _base_open_trade(**overrides) -> dict:
    trade = {
        "pair": "ETH/USDT:USDT",
        "direction": "long",
        "status": "open",
        "entry_price": 3200.0,
        "sl_price": 3100.0,
        "tp_price": 3400.0,
        "position_size": 0.1,
        "leverage_used": 20.0,
        "margin_used": 16.0,
        "risk_amount_usd": 10.0,
        "risk_mode": "percent",
        "leverage_auto_adjusted": False,
        "opened_at": "2025-01-01T10:00:00",
        "created_at": "2025-01-01T09:59:00",
    }
    trade.update(overrides)
    return trade


def test_positions_with_live_data_shows_price_and_pnl():
    """Posisi open dengan data live exchange harus tampil harga sekarang,
    P/L dalam USDT, dan P/L dalam persen — bukan cuma data statis DB."""
    trades = [_base_open_trade()]
    live_positions = {
        "ETH/USDT:USDT": {
            "symbol": "ETH/USDT:USDT",
            "markPrice": 3320.5,
            "unrealizedPnl": 12.05,
            "percentage": 75.3,
            "contracts": 0.1,
        }
    }
    text = fmt_positions(trades, live_positions)
    assert "3320.5" in text
    assert "+12.05 USDT" in text
    assert "+75.30%" in text


def test_positions_live_pnl_fallback_calculation():
    """Kalau ccxt tidak mengirim unrealizedPnl/percentage tapi markPrice ada,
    P/L dihitung manual dari entry_price & position_size sebagai fallback."""
    trades = [_base_open_trade(direction="short", entry_price=3200.0, margin_used=16.0)]
    live_positions = {
        "ETH/USDT:USDT": {
            "symbol": "ETH/USDT:USDT",
            "markPrice": 3100.0,
            "contracts": 0.1,
        }
    }
    text = fmt_positions(trades, live_positions)
    # short: (3200 - 3100) * 0.1 = +10.00 USDT
    assert "+10.00 USDT" in text
    assert "3100" in text


def test_positions_without_live_data_shows_warning():
    """Kalau posisi open tidak ketemu di live_positions (mis. exchange gagal
    diakses), tetap tampil tapi dengan placeholder + warning, bukan error."""
    trades = [_base_open_trade()]
    text = fmt_positions(trades, {})
    assert "Data live exchange tidak tersedia" in text


def test_positions_pending_trade_has_no_pnl_section():
    """Trade berstatus pending belum punya posisi di exchange, jadi tidak
    perlu baris harga/P&L sama sekali."""
    trades = [_base_open_trade(status="pending", opened_at=None)]
    text = fmt_positions(trades, {})
    assert "Harga" not in text
    assert "P/L" not in text


# ── fmt_history ───────────────────────────────────────────────────────────────

def test_history_empty():
    text = fmt_history([])
    assert "Belum ada" in text


def test_history_format():
    trades = [
        {
            "pair": "BTC/USDT:USDT",
            "direction": "short",
            "close_reason": "sl_hit",
            "pnl": -5.0,
            "r_multiple": -1.0,
            "closed_at": "2025-01-01T12:00:00",
        }
    ]
    text = fmt_history(trades)
    assert "BTC/USDT:USDT" in text
    assert "sl_hit" in text
    assert "-5.00" in text


# ── fmt_settings ──────────────────────────────────────────────────────────────

def test_settings_percent_mode():
    s = {
        "risk_mode": "percent", "risk_percent": "1.5", "max_loss_usd": "5.0",
        "bot_paused": "true", "auto_execute_mode": "false",
        "position_conflict_mode": "ask", "liquidation_buffer_pct": "5.0",
        "cb_error_threshold": "3", "cb_window_minutes": "5",
        "default_leverage_cap": "",
    }
    text = fmt_settings(s)
    assert "percent" in text
    assert "1.5%" in text
    assert "PAUSED" in text


def test_settings_fixed_usd_mode():
    s = {
        "risk_mode": "fixed_usd", "risk_percent": "1.0", "max_loss_usd": "10.0",
        "bot_paused": "false", "auto_execute_mode": "true",
        "position_conflict_mode": "skip", "liquidation_buffer_pct": "7.0",
        "cb_error_threshold": "5", "cb_window_minutes": "10",
        "default_leverage_cap": "50",
    }
    text = fmt_settings(s)
    assert "fixed_usd" in text
    assert "$10.0" in text
    assert "RUNNING" in text


# ── fmt_status ────────────────────────────────────────────────────────────────

def test_status_all_closed():
    cb_states = [
        {"component": "telegram_listener", "state": "closed", "consecutive_error_count": 0, "last_error_message": None},
        {"component": "bitget_connection", "state": "closed", "consecutive_error_count": 0, "last_error_message": None},
        {"component": "order_execution",   "state": "open",   "consecutive_error_count": 3, "last_error_message": "timeout"},
        {"component": "signal_parser",     "state": "closed", "consecutive_error_count": 0, "last_error_message": None},
    ]
    db_health = {"status": "healthy", "trade_count": 10, "signal_count": 25}
    text = fmt_status(cb_states, db_health, is_paused=False)
    assert "CLOSED" in text
    assert "OPEN" in text
    assert "timeout" in text
    assert "10 trades" in text