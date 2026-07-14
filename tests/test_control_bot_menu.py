"""
tests/test_control_bot_menu.py
================================
Unit tests untuk bot/control_bot/menu/router.py (Step 10):
  - navigasi submenu (menu:main -> menu:risk -> menu:risk:conflictmode)
  - handle_awaited_text tanpa state aktif (no-op)
  - handle_awaited_text dengan state aktif (panggil handler dengan args benar,
    termasuk pair yang mengandung ":")
Semua network/Telegram call di-mock.
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from bot.control_bot.menu.router import handle_menu_callback, handle_awaited_text
from bot.control_bot.menu.state import AwaitingInput, menu_state

ALLOWED_CHAT_ID = 12345


def _make_callback_update(data: str, chat_id: int = ALLOWED_CHAT_ID):
    update = MagicMock()
    update.callback_query = MagicMock()
    update.callback_query.data = data
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()
    update.effective_chat = MagicMock()
    update.effective_chat.id = chat_id
    return update


def _make_text_update(text: str, chat_id: int = ALLOWED_CHAT_ID):
    update = MagicMock()
    update.effective_chat = MagicMock()
    update.effective_chat.id = chat_id
    update.effective_message = MagicMock()
    update.effective_message.text = text
    return update


def _make_context():
    ctx = MagicMock()
    ctx.args = []
    return ctx


@pytest.fixture(autouse=True)
def _clear_menu_state():
    # Cegah state nyasar antar test.
    yield
    menu_state.clear(ALLOWED_CHAT_ID)


# ── Navigasi submenu ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_navigate_main_to_risk_to_conflictmode():
    ctx = _make_context()
    with patch("bot.control_bot.auth._get_allowed", return_value={ALLOWED_CHAT_ID}):
        update = _make_callback_update("menu:main")
        await handle_menu_callback(update, ctx)
        text, kwargs = update.callback_query.edit_message_text.call_args
        assert "Menu Bot" in text[0]

        update = _make_callback_update("menu:risk")
        await handle_menu_callback(update, ctx)
        text, kwargs = update.callback_query.edit_message_text.call_args
        assert "Risk" in text[0]

        update = _make_callback_update("menu:risk:conflictmode")
        await handle_menu_callback(update, ctx)
        text, kwargs = update.callback_query.edit_message_text.call_args
        assert "Conflict Mode" in text[0]


# ── handle_awaited_text: no state ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_awaited_text_noop_without_state():
    ctx = _make_context()
    with patch("bot.control_bot.auth._get_allowed", return_value={ALLOWED_CHAT_ID}):
        update = _make_text_update("3600")
        await handle_awaited_text(update, ctx)
    # Tidak ada state -> tidak ada balasan apapun yang dikirim.
    update.effective_message.reply_text.assert_not_called()


# ── handle_awaited_text: dengan state aktif ─────────────────────────────────

@pytest.mark.asyncio
async def test_awaited_text_calls_handler_with_prefix_args():
    fake_handler = AsyncMock()
    menu_state.set_awaiting(
        ALLOWED_CHAT_ID,
        AwaitingInput(handler=fake_handler, prefix_args=["BTC/USDT:USDT"], return_menu="menu:pos"),
    )
    ctx = _make_context()
    with patch("bot.control_bot.auth._get_allowed", return_value={ALLOWED_CHAT_ID}):
        update = _make_text_update("65000")
        await handle_awaited_text(update, ctx)

    fake_handler.assert_awaited_once()
    called_update, called_ctx = fake_handler.call_args[0]
    assert called_update is update
    assert called_ctx.args == ["BTC/USDT:USDT", "65000"]
    # State harus sudah dibersihkan setelah dipakai.
    assert menu_state.get_awaiting(ALLOWED_CHAT_ID) is None