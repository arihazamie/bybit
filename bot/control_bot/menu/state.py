"""
bot/control_bot/menu/state.py
==============================
Step 1 — state sederhana per-chat untuk alur "tombol → minta reply teks".

Beberapa command butuh input bebas (persen risk, harga SL/TP, cap leverage)
yang tidak mungkin jadi tombol satu-klik. Alur-nya:

  1. User pilih menu via tombol (mis. "Set Risk %")
  2. Bot simpan state "sedang menunggu input untuk aksi X" + kirim prompt
  3. User balas dengan teks biasa (bukan command)
  4. Router text handler baca state, susun context.args, panggil fungsi
     command lama (cmd_setrisk dkk) apa adanya — tidak perlu tulis ulang
     logic validasi yang sudah ada.

State disimpan in-memory per chat_id. Cukup untuk single control-chat
(sesuai desain bot ini — hanya TELEGRAM_CONTROL_CHAT_ID yang diizinkan).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine

AwaitedHandler = Callable[..., Coroutine[Any, Any, None]]


@dataclass
class AwaitingInput:
    handler: AwaitedHandler          # fungsi cmd_* yang akan dipanggil
    prefix_args: list[str] = field(default_factory=list)  # args yang sudah terkumpul (mis. pair)
    return_menu: str = "menu:main"   # callback_data menu untuk tombol "Batal"


class MenuState:
    def __init__(self) -> None:
        self._awaiting: dict[int, AwaitingInput] = {}

    def set_awaiting(self, chat_id: int, awaiting: AwaitingInput) -> None:
        self._awaiting[chat_id] = awaiting

    def get_awaiting(self, chat_id: int) -> AwaitingInput | None:
        return self._awaiting.get(chat_id)

    def clear(self, chat_id: int) -> None:
        self._awaiting.pop(chat_id, None)


menu_state = MenuState()