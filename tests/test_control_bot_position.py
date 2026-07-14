"""
tests/test_control_bot_position.py
=====================================
Unit test step 17 — handler command manajemen posisi.
Step 18 — update: pending confirmation sekarang lewat pending_store
(TTL-based), bukan dict module-level `_PENDING` yang lama.

Coverage:
  - /settp, /setsl, /setentry  — validasi input + lookup trade
  - /close, /closeall          — validasi + inline keyboard generation
  - /pending                   — tampilkan daftar
  - /cancel                    — lookup pending trade
  - /pause, /resume            — toggle state bot
  - handle_position_callback   — konfirmasi (y), batal (n), expired
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.control_bot.inline.pending_store import make_pending_key, pending_store


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_update(text: str = "", args: list | None = None):
    update = MagicMock()
    update.message = MagicMock()
    update.message.reply_text = AsyncMock()
    update.effective_user = MagicMock()
    update.effective_user.id = 111
    update.effective_chat = MagicMock()
    update.effective_chat.id = 111
    # _send helper pakai update.effective_message (Step 6) — alias supaya
    # assertion lama (update.message.reply_text...) tetap valid.
    update.effective_message = update.message
    return update


def _make_context(args: list | None = None):
    ctx = MagicMock()
    ctx.args = args or []
    ctx.bot = MagicMock()
    ctx.bot.edit_message_text = AsyncMock()
    return ctx


def _make_callback(data: str):
    update = MagicMock()
    update.callback_query = MagicMock()
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()
    update.callback_query.data = data
    return update


def _open_trade(pair: str = "BTC/USDT:USDT", trade_id: int = 1) -> dict:
    return {
        "id": trade_id, "pair": pair, "direction": "long",
        "entry_price": 65000.0, "sl_price": 63000.0,
        "position_size": 0.01, "status": "open",
    }


def _pending_trade(pair: str = "BTC/USDT:USDT", trade_id: int = 2) -> dict:
    return {
        "id": trade_id, "pair": pair, "direction": "long",
        "entry_price": 64000.0, "sl_price": 62000.0,
        "position_size": 0.0, "status": "pending",
    }


@pytest.fixture(autouse=True)
def _mock_reconcile_on_startup():
    """Semua command posisi (setsl/settp/setentry/close/closeall/pending/cancel)
    sekarang jalanin live reconciliation dulu (bot/control_bot/commands/position.py
    baris ~65-66) sebelum aksi. Tanpa mock ini, test bakal coba hit exchange
    beneran (network diblokir sandbox) dan nunggu sampai timeout 8 detik."""
    with patch(
        "bot.executor.order_sync.reconcile_on_startup",
        new_callable=AsyncMock,
    ):
        yield


def _store_pending(action: str, **payload_kwargs) -> str:
    """Helper test: simpan payload langsung ke pending_store (tanpa TTL/timeout)
    supaya bisa dipanggil dari handle_position_callback seperti alur asli."""
    key = make_pending_key()
    pending_store.add(
        key,
        payload={"action": action, "tg_msg_id": 0, **payload_kwargs},
        timeout_seconds=600,
        on_timeout=None,
    )
    return key


ALLOWED_IDS = {111}
AUTH_PATCH = "bot.control_bot.auth._get_allowed"


# ── /settp ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_settp_no_args():
    from bot.control_bot.commands.position import cmd_settp
    update, ctx = _make_update(), _make_context([])
    with patch(AUTH_PATCH, return_value=ALLOWED_IDS):
        await cmd_settp(update, ctx)
    text = update.message.reply_text.call_args[0][0]
    assert "/settp" in text


@pytest.mark.asyncio
async def test_settp_bad_price():
    from bot.control_bot.commands.position import cmd_settp
    update, ctx = _make_update(), _make_context(["BTC/USDT:USDT", "abc"])
    with patch(AUTH_PATCH, return_value=ALLOWED_IDS):
        await cmd_settp(update, ctx)
    text = update.message.reply_text.call_args[0][0]
    assert "angka" in text


@pytest.mark.asyncio
async def test_settp_no_open_trade():
    from bot.control_bot.commands.position import cmd_settp
    update, ctx = _make_update(), _make_context(["BTC/USDT:USDT", "70000"])
    with patch(AUTH_PATCH, return_value=ALLOWED_IDS), \
         patch("bot.control_bot.commands.position.async_get_filled_open_trade_for_pair",
               new_callable=AsyncMock, return_value=None), \
         patch("bot.control_bot.commands.position.async_get_pending_trade_for_pair",
               new_callable=AsyncMock, return_value=None):
        await cmd_settp(update, ctx)
    text = update.message.reply_text.call_args[0][0]
    assert "OPEN" in text


@pytest.mark.asyncio
async def test_settp_shows_confirm_keyboard():
    from bot.control_bot.commands.position import cmd_settp
    update, ctx = _make_update(), _make_context(["BTC/USDT:USDT", "70000"])
    trade = _open_trade()
    with patch(AUTH_PATCH, return_value=ALLOWED_IDS), \
         patch("bot.control_bot.commands.position.async_get_filled_open_trade_for_pair",
               new_callable=AsyncMock, return_value=trade):
        await cmd_settp(update, ctx)
    kwargs = update.message.reply_text.call_args[1]
    assert kwargs.get("reply_markup") is not None


# ── /setsl ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_setsl_shows_confirm():
    from bot.control_bot.commands.position import cmd_setsl
    update, ctx = _make_update(), _make_context(["BTC/USDT:USDT", "63000"])
    trade = _open_trade()
    with patch(AUTH_PATCH, return_value=ALLOWED_IDS), \
         patch("bot.control_bot.commands.position.async_get_filled_open_trade_for_pair",
               new_callable=AsyncMock, return_value=trade):
        await cmd_setsl(update, ctx)
    kwargs = update.message.reply_text.call_args[1]
    assert kwargs.get("reply_markup") is not None
    text = update.message.reply_text.call_args[0][0]
    assert "Stop Loss" in text


# ── /setentry ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_setentry_no_pending():
    from bot.control_bot.commands.position import cmd_setentry
    update, ctx = _make_update(), _make_context(["BTC/USDT:USDT", "68000"])
    with patch(AUTH_PATCH, return_value=ALLOWED_IDS), \
         patch("bot.control_bot.commands.position.async_get_pending_trade_for_pair",
               new_callable=AsyncMock, return_value=None):
        await cmd_setentry(update, ctx)
    text = update.message.reply_text.call_args[0][0]
    assert "PENDING" in text


@pytest.mark.asyncio
async def test_setentry_shows_confirm():
    from bot.control_bot.commands.position import cmd_setentry
    update, ctx = _make_update(), _make_context(["BTC/USDT:USDT", "68000"])
    trade = _pending_trade()
    with patch(AUTH_PATCH, return_value=ALLOWED_IDS), \
         patch("bot.control_bot.commands.position.async_get_pending_trade_for_pair",
               new_callable=AsyncMock, return_value=trade):
        await cmd_setentry(update, ctx)
    kwargs = update.message.reply_text.call_args[1]
    assert kwargs.get("reply_markup") is not None


# ── /close ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_close_no_trade():
    from bot.control_bot.commands.position import cmd_close
    update, ctx = _make_update(), _make_context(["BTC/USDT:USDT"])
    with patch(AUTH_PATCH, return_value=ALLOWED_IDS), \
         patch("bot.control_bot.commands.position.async_get_filled_open_trade_for_pair",
               new_callable=AsyncMock, return_value=None), \
         patch("bot.control_bot.commands.position.async_get_pending_trade_for_pair",
               new_callable=AsyncMock, return_value=None):
        await cmd_close(update, ctx)
    text = update.message.reply_text.call_args[0][0]
    assert "OPEN" in text


@pytest.mark.asyncio
async def test_close_shows_confirm():
    from bot.control_bot.commands.position import cmd_close
    update, ctx = _make_update(), _make_context(["BTC/USDT:USDT"])
    trade = _open_trade()
    with patch(AUTH_PATCH, return_value=ALLOWED_IDS), \
         patch("bot.control_bot.commands.position.async_get_filled_open_trade_for_pair",
               new_callable=AsyncMock, return_value=trade):
        await cmd_close(update, ctx)
    kwargs = update.message.reply_text.call_args[1]
    assert kwargs.get("reply_markup") is not None


# ── /closeall ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_closeall_empty():
    from bot.control_bot.commands.position import cmd_closeall
    update, ctx = _make_update(), _make_context()
    with patch(AUTH_PATCH, return_value=ALLOWED_IDS), \
         patch("bot.control_bot.commands.position.async_get_filled_open_trades",
               new_callable=AsyncMock, return_value=[]):
        await cmd_closeall(update, ctx)
    text = update.message.reply_text.call_args[0][0]
    assert "open" in text.lower()


@pytest.mark.asyncio
async def test_closeall_shows_confirm():
    from bot.control_bot.commands.position import cmd_closeall
    update, ctx = _make_update(), _make_context()
    trades = [_open_trade("BTC/USDT:USDT", 1), _open_trade("ETH/USDT:USDT", 2)]
    with patch(AUTH_PATCH, return_value=ALLOWED_IDS), \
         patch("bot.control_bot.commands.position.async_get_filled_open_trades",
               new_callable=AsyncMock, return_value=trades):
        await cmd_closeall(update, ctx)
    kwargs = update.message.reply_text.call_args[1]
    assert kwargs.get("reply_markup") is not None
    text = update.message.reply_text.call_args[0][0]
    assert "BTC" in text and "ETH" in text


# ── /pending ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pending_empty():
    from bot.control_bot.commands.position import cmd_pending
    update, ctx = _make_update(), _make_context()
    with patch(AUTH_PATCH, return_value=ALLOWED_IDS), \
         patch("bot.control_bot.commands.position.async_get_pending_trades",
               new_callable=AsyncMock, return_value=[]):
        await cmd_pending(update, ctx)
    text = update.message.reply_text.call_args[0][0]
    assert "pending" in text.lower()


@pytest.mark.asyncio
async def test_pending_lists_trades():
    from bot.control_bot.commands.position import cmd_pending
    update, ctx = _make_update(), _make_context()
    trades = [_pending_trade("BTC/USDT:USDT", 2)]
    with patch(AUTH_PATCH, return_value=ALLOWED_IDS), \
         patch("bot.control_bot.commands.position.async_get_pending_trades",
               new_callable=AsyncMock, return_value=trades):
        await cmd_pending(update, ctx)
    text = update.message.reply_text.call_args[0][0]
    assert "BTC" in text


# ── /cancel ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cancel_no_args():
    from bot.control_bot.commands.position import cmd_cancel
    update, ctx = _make_update(), _make_context([])
    with patch(AUTH_PATCH, return_value=ALLOWED_IDS):
        await cmd_cancel(update, ctx)
    text = update.message.reply_text.call_args[0][0]
    assert "/cancel" in text


@pytest.mark.asyncio
async def test_cancel_shows_confirm():
    from bot.control_bot.commands.position import cmd_cancel
    update, ctx = _make_update(), _make_context(["BTC/USDT:USDT"])
    trade = _pending_trade()
    with patch(AUTH_PATCH, return_value=ALLOWED_IDS), \
         patch("bot.control_bot.commands.position.async_get_pending_trade_for_pair",
               new_callable=AsyncMock, return_value=trade):
        await cmd_cancel(update, ctx)
    kwargs = update.message.reply_text.call_args[1]
    assert kwargs.get("reply_markup") is not None


# ── /pause & /resume ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pause_when_running():
    from bot.control_bot.commands.position import cmd_pause
    update, ctx = _make_update(), _make_context()
    with patch(AUTH_PATCH, return_value=ALLOWED_IDS), \
         patch("bot.control_bot.commands.position.is_bot_paused", return_value=False), \
         patch("bot.control_bot.commands.position.set_bot_paused") as mock_pause:
        await cmd_pause(update, ctx)
    mock_pause.assert_called_once_with(True)
    text = update.message.reply_text.call_args[0][0]
    assert "PAUSE" in text


@pytest.mark.asyncio
async def test_pause_already_paused():
    from bot.control_bot.commands.position import cmd_pause
    update, ctx = _make_update(), _make_context()
    with patch(AUTH_PATCH, return_value=ALLOWED_IDS), \
         patch("bot.control_bot.commands.position.is_bot_paused", return_value=True):
        await cmd_pause(update, ctx)
    text = update.message.reply_text.call_args[0][0]
    assert "sudah" in text.lower()


@pytest.mark.asyncio
async def test_resume_when_paused():
    from bot.control_bot.commands.position import cmd_resume
    update, ctx = _make_update(), _make_context()
    mock_cb = MagicMock()
    mock_cb.resume = AsyncMock(return_value=[])
    with patch(AUTH_PATCH, return_value=ALLOWED_IDS), \
         patch("bot.control_bot.commands.position.is_bot_paused", return_value=True), \
         patch("bot.control_bot.commands.position.set_bot_paused") as mock_resume, \
         patch("bot.control_bot.commands.position.get_circuit_breaker", return_value=mock_cb):
        await cmd_resume(update, ctx)
    mock_resume.assert_called_once_with(False)
    mock_cb.resume.assert_awaited_once()
    text = update.message.reply_text.call_args[0][0]
    assert "AKTIF" in text


@pytest.mark.asyncio
async def test_resume_already_running():
    from bot.control_bot.commands.position import cmd_resume
    update, ctx = _make_update(), _make_context()
    mock_cb = MagicMock()
    mock_cb.resume = AsyncMock(return_value=[])
    with patch(AUTH_PATCH, return_value=ALLOWED_IDS), \
         patch("bot.control_bot.commands.position.is_bot_paused", return_value=False), \
         patch("bot.control_bot.commands.position.get_circuit_breaker", return_value=mock_cb):
        await cmd_resume(update, ctx)
    mock_cb.resume.assert_awaited_once()
    text = update.message.reply_text.call_args[0][0]
    assert "AKTIF" in text
    assert "Tidak ada circuit breaker" in text


@pytest.mark.asyncio
async def test_resume_also_transitions_open_circuit_breaker():
    """
    Regresi: /resume WAJIB juga meng-HALF_OPEN-kan circuit breaker yang
    trip, bukan cuma toggle is_bot_paused — sebelumnya command ini tidak
    pernah menyentuh circuit breaker sama sekali, jadi user yang CB-nya
    OPEN tetap terblokir eksekusi walau sudah kirim /resume.
    """
    from bot.control_bot.commands.position import cmd_resume
    update, ctx = _make_update(), _make_context()
    mock_cb = MagicMock()
    mock_cb.resume = AsyncMock(return_value=["order_execution"])
    with patch(AUTH_PATCH, return_value=ALLOWED_IDS), \
         patch("bot.control_bot.commands.position.is_bot_paused", return_value=False), \
         patch("bot.control_bot.commands.position.get_circuit_breaker", return_value=mock_cb):
        await cmd_resume(update, ctx)
    mock_cb.resume.assert_awaited_once_with()
    text = update.message.reply_text.call_args[0][0]
    assert "order_execution" in text
    assert "HALF_OPEN" in text


# ── Callback handler ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_callback_cancel_choice():
    from bot.control_bot.commands.position import handle_position_callback
    key = _store_pending("close", pair="BTC/USDT:USDT", trade_id=1)
    update = _make_callback(f"pos:{key}:n")
    ctx = _make_context()
    await handle_position_callback(update, ctx)
    text = update.callback_query.edit_message_text.call_args[0][0]
    assert "Dibatalkan" in text
    assert not pending_store.has(key)


@pytest.mark.asyncio
async def test_callback_expired_key():
    from bot.control_bot.commands.position import handle_position_callback
    update = _make_callback("pos:deadbeef:y")
    ctx = _make_context()
    await handle_position_callback(update, ctx)
    text = update.callback_query.edit_message_text.call_args[0][0]
    assert "kedaluwarsa" in text.lower()


@pytest.mark.asyncio
async def test_callback_confirm_close():
    from bot.control_bot.commands.position import handle_position_callback
    from bot.executor.order_manager import OrderManagementResult
    key = _store_pending("close", pair="BTC/USDT:USDT", trade_id=1)
    update = _make_callback(f"pos:{key}:y")
    ctx = _make_context()
    mock_result = OrderManagementResult(
        success=True, operation="close_position", pair="BTC/USDT:USDT",
        trade_id=1, closed_pnl=12.5, is_dry_run=True,
    )
    with patch("bot.control_bot.commands.position.close_position",
               new_callable=AsyncMock, return_value=mock_result):
        await handle_position_callback(update, ctx)
    text = update.callback_query.edit_message_text.call_args[0][0]
    assert "closed" in text.lower() or "Posisi" in text


@pytest.mark.asyncio
async def test_callback_confirm_settp():
    from bot.control_bot.commands.position import handle_position_callback
    key = _store_pending("settp", pair="BTC/USDT:USDT", trade_id=1, price=70000.0)
    update = _make_callback(f"pos:{key}:y")
    ctx = _make_context()
    with patch("bot.control_bot.commands.position.set_take_profit",
               new_callable=AsyncMock,
               return_value=MagicMock(success=True, is_dry_run=False, notes=[], failure_reason=None)):
        await handle_position_callback(update, ctx)
    text = update.callback_query.edit_message_text.call_args[0][0]
    assert "Take Profit" in text


@pytest.mark.asyncio
async def test_callback_confirm_pause_not_a_callback_action():
    """pause/resume tidak melewati inline button — cmd_pause tidak pernah
    menyimpan payload apapun ke pending_store."""
    from bot.control_bot.commands.position import cmd_pause
    update, ctx = _make_update(), _make_context()
    with patch(AUTH_PATCH, return_value=ALLOWED_IDS), \
         patch("bot.control_bot.commands.position.is_bot_paused", return_value=False), \
         patch("bot.control_bot.commands.position.set_bot_paused"), \
         patch("bot.control_bot.inline.pending_store.pending_store.add") as mock_add:
        await cmd_pause(update, ctx)
    mock_add.assert_not_called()


@pytest.mark.asyncio
async def test_callback_unknown_prefix_ignored():
    from bot.control_bot.commands.position import handle_position_callback
    update = _make_callback("other:abc:y")
    ctx = _make_context()
    await handle_position_callback(update, ctx)
    update.callback_query.edit_message_text.assert_not_called()