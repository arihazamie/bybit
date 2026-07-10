"""
bot/control_bot/inline/pending_store.py
=========================================
TTL-based store untuk semua pending inline-button confirmation.

Singleton `pending_store` dipakai oleh:
  - commands/position.py  (settp, setsl, close, closeall, cancel, setentry)
  - inline/signal_confirm.py (sinyal ambigu)
  - inline/conflict_confirm.py (konflik posisi)

API publik:
  pending_store.add(key, payload, timeout_seconds, on_timeout)
  pending_store.pop(key)  → dict | None
  make_pending_key()      → str (8-char hex)
"""

from __future__ import annotations

import asyncio
import uuid
import logging
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

OnTimeoutFn = Callable[[str, dict], Awaitable[None]]


class PendingStore:
    def __init__(self) -> None:
        self._data:  dict[str, dict]          = {}
        self._tasks: dict[str, asyncio.Task]  = {}

    def add(
        self,
        key: str,
        payload: dict[str, Any],
        timeout_seconds: float,
        on_timeout: Optional[OnTimeoutFn] = None,
    ) -> None:
        """Simpan payload dengan TTL. on_timeout dipanggil async saat expired."""
        self._data[key] = payload
        if on_timeout is not None:
            task = asyncio.create_task(self._expire(key, timeout_seconds, on_timeout))
            self._tasks[key] = task

    def pop(self, key: str) -> Optional[dict]:
        """Ambil dan hapus payload; cancel timer TTL jika ada."""
        payload = self._data.pop(key, None)
        task    = self._tasks.pop(key, None)
        if task is not None:
            task.cancel()
        return payload

    def has(self, key: str) -> bool:
        return key in self._data

    async def _expire(self, key: str, delay: float, on_timeout: OnTimeoutFn) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        payload = self._data.pop(key, None)
        self._tasks.pop(key, None)
        if payload is not None:
            try:
                await on_timeout(key, payload)
            except Exception as exc:
                logger.error("[pending_store] on_timeout error key=%s: %s", key, exc)


def make_pending_key() -> str:
    return uuid.uuid4().hex[:8]


# Singleton
pending_store = PendingStore()
