"""
tests/test_control_bot_risk.py
================================
Unit tests untuk Step 16 — risk & leverage command handlers.
Handler di-test via DB in-memory; tidak ada call ke Telegram atau exchange.
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from db.database import init_db, set_db_path
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


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    set_db_path(tmp_path / "test.db")
    init_db()
    yield


def _make_update(text: str = "", args: list[str] | None = None):
    update = MagicMock()
    update.effective_chat.id = 12345
    update.message.reply_text = AsyncMock()
    update.message.text = text
    # _send helper pakai update.effective_message (Step 6) — alias supaya
    # assertion lama (update.message.reply_text...) tetap valid.
    update.effective_message = update.message
    return update


def _make_context(args: list[str] | None = None):
    ctx = MagicMock()
    ctx.args = args or []
    return ctx


# ── /setrisk ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_setrisk_valid():
    from bot.control_bot.commands.risk import cmd_setrisk
    with patch("bot.control_bot.auth._get_allowed", return_value={12345}):
        await cmd_setrisk(_make_update(), _make_context(["2.5"]))
    assert get_risk_mode() == "percent"
    assert get_risk_percent() == 2.5


@pytest.mark.asyncio
async def test_setrisk_no_args():
    from bot.control_bot.commands.risk import cmd_setrisk
    with patch("bot.control_bot.auth._get_allowed", return_value={12345}):
        update = _make_update()
        await cmd_setrisk(update, _make_context([]))
        reply = update.message.reply_text.call_args[0][0]
        assert "Format" in reply


@pytest.mark.asyncio
async def test_setrisk_invalid_value():
    from bot.control_bot.commands.risk import cmd_setrisk
    with patch("bot.control_bot.auth._get_allowed", return_value={12345}):
        update = _make_update()
        await cmd_setrisk(update, _make_context(["abc"]))
        reply = update.message.reply_text.call_args[0][0]
        assert "❌" in reply


@pytest.mark.asyncio
async def test_setrisk_out_of_range():
    from bot.control_bot.commands.risk import cmd_setrisk
    with patch("bot.control_bot.auth._get_allowed", return_value={12345}):
        update = _make_update()
        await cmd_setrisk(update, _make_context(["150"]))
        reply = update.message.reply_text.call_args[0][0]
        assert "❌" in reply


# ── /setmaxloss ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_setmaxloss_valid():
    from bot.control_bot.commands.risk import cmd_setmaxloss
    with patch("bot.control_bot.auth._get_allowed", return_value={12345}):
        await cmd_setmaxloss(_make_update(), _make_context(["10"]))
    assert get_risk_mode() == "fixed_usd"
    assert get_max_loss_usd() == 10.0


@pytest.mark.asyncio
async def test_setmaxloss_zero():
    from bot.control_bot.commands.risk import cmd_setmaxloss
    with patch("bot.control_bot.auth._get_allowed", return_value={12345}):
        update = _make_update()
        await cmd_setmaxloss(update, _make_context(["0"]))
        reply = update.message.reply_text.call_args[0][0]
        assert "❌" in reply


# ── /riskmode ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_riskmode_shows_active_mode():
    from bot.control_bot.commands.risk import cmd_riskmode
    set_setting("risk_mode", "fixed_usd")
    set_setting("max_loss_usd", "7.5")
    with patch("bot.control_bot.auth._get_allowed", return_value={12345}):
        update = _make_update()
        await cmd_riskmode(update, _make_context([]))
        reply = update.message.reply_text.call_args[0][0]
        assert "Fixed USD" in reply
        assert "7.5" in reply


# ── Leverage cap CRUD ─────────────────────────────────────────────────────────

def test_set_leverage_cap_pair_specific():
    set_leverage_cap("BTC/USDT:USDT", 50.0)
    assert get_leverage_cap("BTC/USDT:USDT") == 50.0
    assert get_leverage_cap("ETH/USDT:USDT") is None


def test_set_leverage_cap_zero_removes_cap():
    set_leverage_cap("BTC/USDT:USDT", 50.0)
    set_leverage_cap("BTC/USDT:USDT", 0)
    assert get_leverage_cap("BTC/USDT:USDT") is None


def test_get_all_leverage_caps():
    set_leverage_cap("BTC/USDT:USDT", 50.0)
    set_leverage_cap("ETH/USDT:USDT", 25.0)
    caps = get_all_leverage_caps()
    assert caps["BTC/USDT:USDT"] == 50.0
    assert caps["ETH/USDT:USDT"] == 25.0


def test_global_cap_fallback():
    set_setting("default_leverage_cap", "30")
    # pair-specific tidak ada → fallback ke global
    assert get_leverage_cap("XRP/USDT:USDT") == 30.0
    # pair-specific override global
    set_leverage_cap("XRP/USDT:USDT", 10.0)
    assert get_leverage_cap("XRP/USDT:USDT") == 10.0


# ── /setleverage ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_setleverage_valid():
    from bot.control_bot.commands.risk import cmd_setleverage
    with patch("bot.control_bot.auth._get_allowed", return_value={12345}):
        update = _make_update()
        await cmd_setleverage(update, _make_context(["BTC/USDT:USDT", "50"]))
        reply = update.message.reply_text.call_args[0][0]
        assert "50x" in reply
    assert get_leverage_cap("BTC/USDT:USDT") == 50.0


@pytest.mark.asyncio
async def test_setleverage_zero_removes():
    from bot.control_bot.commands.risk import cmd_setleverage
    set_leverage_cap("BTC/USDT:USDT", 50.0)
    with patch("bot.control_bot.auth._get_allowed", return_value={12345}):
        update = _make_update()
        await cmd_setleverage(update, _make_context(["BTC/USDT:USDT", "0"]))
        reply = update.message.reply_text.call_args[0][0]
        assert "dihapus" in reply
    assert get_leverage_cap("BTC/USDT:USDT") is None


@pytest.mark.asyncio
async def test_setleverage_missing_args():
    from bot.control_bot.commands.risk import cmd_setleverage
    with patch("bot.control_bot.auth._get_allowed", return_value={12345}):
        update = _make_update()
        await cmd_setleverage(update, _make_context(["BTC/USDT:USDT"]))
        reply = update.message.reply_text.call_args[0][0]
        assert "Format" in reply


# ── /leverage (no exchange call) ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_leverage_no_args_no_caps():
    from bot.control_bot.commands.risk import cmd_leverage
    with patch("bot.control_bot.auth._get_allowed", return_value={12345}):
        update = _make_update()
        await cmd_leverage(update, _make_context([]))
        reply = update.message.reply_text.call_args[0][0]
        assert "Tidak ada leverage cap" in reply


@pytest.mark.asyncio
async def test_leverage_no_args_with_caps():
    from bot.control_bot.commands.risk import cmd_leverage
    set_leverage_cap("BTC/USDT:USDT", 50.0)
    with patch("bot.control_bot.auth._get_allowed", return_value={12345}):
        update = _make_update()
        await cmd_leverage(update, _make_context([]))
        reply = update.message.reply_text.call_args[0][0]
        assert "50x" in reply


@pytest.mark.asyncio
async def test_leverage_pair_exchange_fail():
    """Jika exchange gagal, cap manual tetap ditampilkan."""
    from bot.control_bot.commands.risk import cmd_leverage
    set_leverage_cap("BTC/USDT:USDT", 75.0)

    # Buat mock module agar tidak perlu ccxt terinstal
    import sys
    from unittest.mock import MagicMock

    fake_client = AsyncMock()
    fake_client.get_max_leverage = AsyncMock(side_effect=Exception("network error"))
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=False)

    fake_module = MagicMock()
    fake_module.BitgetRestClient = MagicMock(return_value=fake_client)

    with patch("bot.control_bot.auth._get_allowed", return_value={12345}):
        with patch.dict(sys.modules, {"exchange.bitget.rest_client": fake_module}):
            update = _make_update()
            await cmd_leverage(update, _make_context(["BTC/USDT:USDT"]))
            reply = update.message.reply_text.call_args[0][0]
            assert "BTC/USDT:USDT" in reply
            assert "75x" in reply


# ── /conflictmode ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_conflictmode_valid():
    from bot.control_bot.commands.risk import cmd_conflictmode
    with patch("bot.control_bot.auth._get_allowed", return_value={12345}):
        await cmd_conflictmode(_make_update(), _make_context(["skip"]))
    assert get_position_conflict_mode() == "skip"


@pytest.mark.asyncio
async def test_conflictmode_invalid():
    from bot.control_bot.commands.risk import cmd_conflictmode
    with patch("bot.control_bot.auth._get_allowed", return_value={12345}):
        update = _make_update()
        await cmd_conflictmode(update, _make_context(["invalid_mode"]))
        reply = update.message.reply_text.call_args[0][0]
        assert "❌" in reply


@pytest.mark.asyncio
async def test_conflictmode_no_args_shows_current():
    from bot.control_bot.commands.risk import cmd_conflictmode
    set_setting("position_conflict_mode", "replace")
    with patch("bot.control_bot.auth._get_allowed", return_value={12345}):
        update = _make_update()
        await cmd_conflictmode(update, _make_context([]))
        reply = update.message.reply_text.call_args[0][0]
        assert "replace" in reply
        assert "ask" in reply  # semua mode ditampilkan


# ── Auth check ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_unauthorized_request_ignored():
    from bot.control_bot.commands.risk import cmd_setrisk
    with patch("bot.control_bot.auth._get_allowed", return_value={99999}):
        update = _make_update()  # chat_id = 12345, bukan 99999
        await cmd_setrisk(update, _make_context(["1.0"]))
        update.message.reply_text.assert_not_called()