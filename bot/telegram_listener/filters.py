"""
bot/telegram_listener/filters.py
=================================
Logika filter untuk memastikan hanya pesan dari:
  - Grup: "TRADING HUB | VIP CC"  (cocokkan by title atau username)
  - Topic: [FUTURES] - Signals    (cocokkan by reply_to.reply_to_top_id == SIGNAL_TOPIC_ID)

Dipakai oleh listener.py sebelum meneruskan pesan ke parser.
"""

from __future__ import annotations

from telethon.tl.types import (
    Message,
    PeerChannel,
    PeerChat,
    MessageReplyHeader,
)

from core.logging_setup import get_logger

logger = get_logger(__name__)


def is_target_group(event_chat, group_name: str) -> bool:
    """
    Cek apakah chat adalah grup sinyal target.

    Cocokkan berdasarkan:
    - chat.title (nama grup persis)
    - chat.username (username publik grup, tanpa @)

    Args:
        event_chat: objek chat dari event Telethon (bisa None)
        group_name: nama grup dari settings (SIGNAL_GROUP_NAME)

    Returns:
        True jika ini adalah grup target
    """
    if event_chat is None:
        return False

    title: str = getattr(event_chat, "title", "") or ""
    username: str = getattr(event_chat, "username", "") or ""

    # Cocokkan nama persis (case-insensitive) atau username
    match_title    = title.strip().lower() == group_name.strip().lower()
    match_username = username.strip().lower() == group_name.strip().lstrip("@").lower()

    if match_title or match_username:
        return True

    return False


def is_target_topic(message: Message, topic_id: int) -> bool:
    """
    Cek apakah pesan berada di topic yang benar (SIGNAL_TOPIC_ID).

    Di Telegram Forum/Topics, setiap pesan punya reply_to yang berisi
    reply_to_top_id = ID pesan pembuka topic tersebut.

    Args:
        message: objek Message dari Telethon
        topic_id: topic ID dari settings (SIGNAL_TOPIC_ID)

    Returns:
        True jika pesan berada di topic target
    """
    if topic_id == 0:
        # Jika topic_id belum dikonfigurasi, loloskan semua (untuk dev/testing)
        logger.warning(
            "SIGNAL_TOPIC_ID belum dikonfigurasi (= 0) — "
            "semua pesan dari grup akan diloloskan. Set SIGNAL_TOPIC_ID di .env!"
        )
        return True

    reply_to: MessageReplyHeader | None = getattr(message, "reply_to", None)
    if reply_to is None:
        return False

    # reply_to_top_id → ID pesan pembuka topic (forum thread root)
    top_id: int | None = getattr(reply_to, "reply_to_top_id", None)
    # Fallback: beberapa versi Telegram menggunakan reply_to_msg_id untuk topic root
    msg_id: int | None = getattr(reply_to, "reply_to_msg_id", None)

    return top_id == topic_id or msg_id == topic_id


def should_process(message: Message, chat, group_name: str, topic_id: int) -> bool:
    """
    Gate utama: gabungkan semua filter.

    Returns True hanya jika:
    1. Pesan dari grup target
    2. Pesan ada di topic target
    3. Pesan bukan dari bot sendiri (tidak ada self-loop)
    4. Pesan punya konten teks (bukan sticker/media tanpa caption)

    Args:
        message:    objek Message Telethon
        chat:       objek chat Telethon
        group_name: SIGNAL_GROUP_NAME dari settings
        topic_id:   SIGNAL_TOPIC_ID dari settings
    """
    # Filter 1: grup yang benar
    if not is_target_group(chat, group_name):
        return False

    # Filter 2: topic yang benar
    if not is_target_topic(message, topic_id):
        return False

    # Filter 3: harus ada teks (sinyal selalu punya teks)
    text: str = getattr(message, "text", "") or ""
    if not text.strip():
        logger.debug(
            "Pesan #%s dilewati — tidak ada teks (mungkin sticker/media)",
            message.id,
        )
        return False

    return True
