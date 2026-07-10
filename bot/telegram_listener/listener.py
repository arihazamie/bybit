"""
bot/telegram_listener/listener.py
===================================
Telethon user-session listener untuk membaca sinyal dari grup Telegram privat.

Alur:
1. Login dengan akun pribadi user (bukan bot API) — karena grup privat
   tidak bisa dibaca oleh bot yang tidak di-invite
2. Filter pesan masuk → hanya dari grup & topic yang dikonfigurasi
3. Log semua pesan yang lolos filter secara lengkap (raw text + metadata)
   ke file log — BELUM parsing apapun di step ini
4. Teruskan pesan ke callback (akan disambung ke parser di Step 4)

Catatan keamanan:
- Session file Telethon (.session) menyimpan kredensial login — JANGAN commit ke git
- Akun ini hanya dipakai sebagai "pendengar pasif" — tidak pernah kirim pesan
  dari akun ini untuk menghindari risiko flag spam oleh Telegram
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Callable, Awaitable

from telethon import TelegramClient, events
from telethon.errors import (
    SessionPasswordNeededError,
    FloodWaitError,
    AuthKeyUnregisteredError,
)
from telethon.tl.types import Message

from config.settings import settings
from core.logging_setup import get_logger
from core.constants import Component
from bot.telegram_listener.filters import should_process

logger = get_logger(__name__)

# Type alias untuk callback yang menerima raw pesan
RawMessageCallback = Callable[[dict], Awaitable[None]]


class TelegramListener:
    """
    Wrapper Telethon yang mengelola koneksi, reconnect, dan filtering pesan.

    Gunakan sebagai async context manager atau panggil start() / stop() manual.
    """

    def __init__(self, on_message: RawMessageCallback | None = None) -> None:
        """
        Args:
            on_message: async callback dipanggil setiap ada pesan yang lolos filter.
                        Menerima dict raw_event (lihat _build_raw_event).
                        Jika None, pesan hanya di-log (berguna untuk testing).
        """
        self._client: TelegramClient | None = None
        self._on_message = on_message
        self._running = False

    # ── Koneksi ─────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Inisialisasi dan jalankan Telethon client."""
        logger.info("[%s] Memulai Telethon listener...", Component.TELEGRAM_LISTENER)

        session_path = "data/telethon_session"

        self._client = TelegramClient(
            session=session_path,
            api_id=settings.TELEGRAM_API_ID,
            api_hash=settings.TELEGRAM_API_HASH,
            # Batasi koneksi — akun pribadi, bukan bot server
            connection_retries=5,
            retry_delay=5,
            auto_reconnect=True,
        )

        try:
            await self._client.start(phone=settings.TELEGRAM_PHONE)
        except SessionPasswordNeededError:
            logger.critical(
                "[%s] Akun menggunakan 2FA (Two-Step Verification). "
                "Tambahkan password 2FA ke flow start() atau gunakan session yang sudah login.",
                Component.TELEGRAM_LISTENER,
            )
            raise
        except AuthKeyUnregisteredError:
            logger.critical(
                "[%s] Session tidak valid atau sudah di-revoke. "
                "Hapus file data/telethon_session.session dan login ulang.",
                Component.TELEGRAM_LISTENER,
            )
            raise

        me = await self._client.get_me()
        logger.info(
            "[%s] Login berhasil sebagai: %s %s (id=%s)",
            Component.TELEGRAM_LISTENER,
            getattr(me, "first_name", ""),
            getattr(me, "last_name", "") or "",
            me.id,
        )

        # Daftarkan event handler untuk pesan baru
        self._client.add_event_handler(
            self._handle_new_message,
            events.NewMessage(),
        )

        self._running = True
        logger.info(
            "[%s] Listener aktif — menunggu pesan dari grup '%s' topic_id=%s",
            Component.TELEGRAM_LISTENER,
            settings.SIGNAL_GROUP_NAME,
            settings.SIGNAL_TOPIC_ID,
        )

    async def run_until_disconnected(self) -> None:
        """Block sampai client disconnect (loop utama listener)."""
        if self._client is None:
            raise RuntimeError("Panggil start() dulu sebelum run_until_disconnected()")
        await self._client.run_until_disconnected()

    async def stop(self) -> None:
        """Disconnect client dengan bersih."""
        self._running = False
        if self._client and self._client.is_connected():
            await self._client.disconnect()
            logger.info("[%s] Listener disconnected.", Component.TELEGRAM_LISTENER)

    # ── Event handler ────────────────────────────────────────────────────

    async def _handle_new_message(self, event: events.NewMessage.Event) -> None:
        """
        Dipanggil Telethon setiap ada pesan baru di semua chat.
        Filter dulu — hanya proses jika dari grup & topic yang benar.
        """
        try:
            message: Message = event.message
            chat = await event.get_chat()

            # Terapkan filter grup + topic + teks
            if not should_process(
                message=message,
                chat=chat,
                group_name=settings.SIGNAL_GROUP_NAME,
                topic_id=settings.SIGNAL_TOPIC_ID,
            ):
                return

            # Pesan lolos filter — bangun raw event dan log
            raw_event = self._build_raw_event(message, chat)
            self._log_raw_message(raw_event)

            # Teruskan ke callback (akan disambung ke parser di Step 4)
            if self._on_message is not None:
                await self._on_message(raw_event)

        except FloodWaitError as e:
            logger.warning(
                "[%s] FloodWaitError — Telegram minta tunggu %s detik",
                Component.TELEGRAM_LISTENER,
                e.seconds,
            )
            await asyncio.sleep(e.seconds)
        except Exception as exc:
            logger.exception(
                "[%s] Error tidak terduga saat handle pesan: %s",
                Component.TELEGRAM_LISTENER,
                exc,
            )

    # ── Helpers ──────────────────────────────────────────────────────────

    def _build_raw_event(self, message: Message, chat) -> dict:
        """
        Bangun dict standar dari raw pesan Telethon.
        Ini yang akan diteruskan ke parser di step berikutnya.

        Keys:
            message_id   : int   — ID unik pesan di Telegram
            chat_id      : int   — ID grup/channel
            chat_title   : str   — Nama grup
            topic_id     : int   — ID topic (reply_to_top_id)
            sender_id    : int   — ID pengirim
            sender_name  : str   — Nama pengirim (first + last)
            sender_username: str — Username pengirim (tanpa @)
            text         : str   — Teks pesan mentah
            received_at  : str   — Timestamp UTC ISO-8601
        """
        reply_to = getattr(message, "reply_to", None)
        topic_id = 0
        if reply_to is not None:
            topic_id = (
                getattr(reply_to, "reply_to_top_id", None)
                or getattr(reply_to, "reply_to_msg_id", None)
                or 0
            )

        sender = getattr(message, "sender", None)
        sender_id       = getattr(sender, "id", 0) if sender else 0
        sender_fname    = getattr(sender, "first_name", "") or ""
        sender_lname    = getattr(sender, "last_name", "") or ""
        sender_username = getattr(sender, "username", "") or ""
        sender_name     = f"{sender_fname} {sender_lname}".strip()

        return {
            "message_id":       message.id,
            "chat_id":          getattr(chat, "id", 0),
            "chat_title":       getattr(chat, "title", "") or "",
            "topic_id":         topic_id,
            "sender_id":        sender_id,
            "sender_name":      sender_name,
            "sender_username":  sender_username,
            "text":             message.text or "",
            "received_at":      datetime.now(timezone.utc).isoformat(),
        }

    def _log_raw_message(self, raw: dict) -> None:
        """Log pesan mentah lengkap ke file log (INFO level)."""
        logger.info(
            "[RAW SIGNAL] message_id=%s | from=%s (@%s) | topic=%s | received=%s\n"
            "─── TEXT START ───\n%s\n─── TEXT END ───",
            raw["message_id"],
            raw["sender_name"],
            raw["sender_username"],
            raw["topic_id"],
            raw["received_at"],
            raw["text"],
        )


# ── Factory function ─────────────────────────────────────────────────────

async def start_listener(on_message: RawMessageCallback | None = None) -> TelegramListener:
    """
    Buat dan jalankan TelegramListener.

    Dipanggil dari main.py sebagai asyncio task.

    Args:
        on_message: callback async untuk pesan yang lolos filter.
                    Akan disambung ke signal parser di Step 4.

    Returns:
        Instance TelegramListener yang sudah running.
    """
    listener = TelegramListener(on_message=on_message)
    await listener.start()
    return listener
