"""
bot/circuit_breaker/manager.py
===============================
Step 14 — Circuit Breaker state machine & retry integration.

State machine per komponen:
    CLOSED    → normal, operasi jalan
    OPEN      → trip (N critical error dalam T menit)
                eksekusi sinyal baru BERHENTI
                posisi monitoring TETAP JALAN (is_position_monitor=True)
    HALF_OPEN → setelah /resume, coba 1 probe operation
                sukses  → CLOSED
                gagal   → OPEN kembali

Aturan:
  - Tidak auto-resume — wajib /resume manual dari Telegram
  - Trip threshold: N=3 critical error dalam T=5 menit (dari settings)
  - Transient error di-retry oleh @with_retry di retry.py
    Circuit breaker hanya merespon CriticalError (setelah retry habis)
  - Alert Telegram prioritas tinggi saat trip
  - Loop monitoring posisi TIDAK ikut berhenti saat trip
"""

from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Optional, TypeVar

from core.constants import CBState, Component, EventType, Severity
from core.logging_setup import get_logger
from db.crud.circuit_breaker import (
    async_get_cb_state,
    async_get_cb_summary_for_dashboard,
    async_is_cb_open,
    async_record_error,
    async_reset_circuit_breaker,
    async_reset_error_count,
    async_resume_all_components,
    async_transition_to_half_open,
    async_trip_circuit_breaker,
    get_cb_state,
    is_cb_open,
)
from db.crud.event_log import async_log_event
from exchange.bitget.retry import CriticalError, TransientError

T = TypeVar("T")
logger = get_logger(__name__)

NotifyFn = Callable[[str], Awaitable[None]]


class CBOpenError(Exception):
    """Raised saat operasi ditolak karena circuit breaker OPEN."""


class CircuitBreakerManager:
    """
    Singleton manager circuit breaker untuk semua komponen bot.

    Usage:
        cb = get_circuit_breaker()
        cb.set_notify_fn(telegram_send)           # wired at startup (Step 19)

        # Wrap operasi dengan CB:
        result = await cb.execute_with_cb(
            Component.ORDER_EXECUTION,
            some_async_fn(),
        )

        # Monitoring posisi — tidak pernah diblokir:
        result = await cb.execute_with_cb(
            Component.BITGET_CONNECTION,
            watch_positions_coro(),
            is_position_monitor=True,
        )

        # /resume command dari Telegram:
        transitioned = await cb.resume()
    """

    def __init__(self, error_threshold: int = 3, window_minutes: int = 5) -> None:
        self._threshold = error_threshold
        self._window = timedelta(minutes=window_minutes)
        # In-memory deque per komponen: timestamp error dalam sliding window
        self._error_times: dict[str, deque[datetime]] = defaultdict(deque)
        # Notify fn di-inject dari luar setelah Telegram control bot ready
        self._notify_fn: Optional[NotifyFn] = None
        # Per-komponen lock — hindari race condition trip bersamaan
        self._locks: dict[str, asyncio.Lock] = {
            c: asyncio.Lock() for c in Component.ALL
        }

    # ── Public API ────────────────────────────────────────────────────────────

    def set_notify_fn(self, fn: NotifyFn) -> None:
        """Inject callback untuk kirim alert Telegram. Dipanggil saat bot ready."""
        self._notify_fn = fn

    async def on_error(self, component: str, error_message: str) -> None:
        """
        Catat satu critical error untuk komponen.
        Jika threshold (N error dalam T menit) tercapai dan state masih CLOSED
        → trip ke OPEN dan kirim alert Telegram.
        """
        lock = self._locks.get(component, asyncio.Lock())
        async with lock:
            now = datetime.now(timezone.utc)
            self._error_times[component].append(now)
            self._prune_window(component)

            count = len(self._error_times[component])
            await async_record_error(component, error_message)

            logger.warning(
                "[cb] %s: error %d/%d dalam %.0f menit | %s",
                component, count, self._threshold,
                self._window.total_seconds() / 60,
                error_message[:120],
            )

            if count >= self._threshold:
                state_rec = await async_get_cb_state(component)
                current = state_rec["state"] if state_rec else CBState.CLOSED
                if current == CBState.CLOSED:
                    await self._trip(component, error_message)

    async def on_success(self, component: str) -> None:
        """
        Catat operasi sukses:
        - HALF_OPEN → probe berhasil → CLOSED + notif
        - CLOSED    → reset error counter jika window kosong
        """
        state_rec = await async_get_cb_state(component)
        if not state_rec:
            return
        state = state_rec["state"]

        if state == CBState.HALF_OPEN:
            await async_reset_circuit_breaker(component)
            self._error_times[component].clear()
            await async_log_event(
                EventType.CIRCUIT_BREAKER_RESET,
                f"CB RESET: [{component}] HALF_OPEN → CLOSED (probe sukses)",
                component=component,
                severity=Severity.INFO,
            )
            logger.info("[cb] ✅ %s: HALF_OPEN → CLOSED (probe sukses)", component)
            await self._notify(
                f"✅ Circuit breaker RESET: *{component}*\n"
                f"Probe berhasil — komponen kembali NORMAL (CLOSED)."
            )

        elif state == CBState.CLOSED:
            self._prune_window(component)
            if not self._error_times[component]:
                await async_reset_error_count(component)

    async def on_halfopen_failure(self, component: str, error_message: str) -> None:
        """
        Probe di HALF_OPEN gagal → kembali ke OPEN + notif.
        Dipanggil oleh execute_with_cb secara otomatis.
        """
        await async_trip_circuit_breaker(component, error_message)
        await async_log_event(
            EventType.CIRCUIT_BREAKER_TRIP,
            f"CB PROBE GAGAL: [{component}] kembali OPEN — {error_message[:200]}",
            component=component,
            severity=Severity.CRITICAL,
        )
        logger.error("[cb] 🔴 %s: probe GAGAL → kembali OPEN", component)
        await self._notify(
            f"🔴 Circuit breaker probe *GAGAL*: {component}\n"
            f"Komponen kembali ke OPEN.\n"
            f"Error: {error_message[:150]}\n"
            f"Kirim /resume lagi setelah masalah dipastikan selesai."
        )

    async def resume(self, component: Optional[str] = None) -> list[str]:
        """
        Transisi OPEN → HALF_OPEN (dipanggil oleh /resume command).
        component=None → resume semua komponen yang OPEN.
        Return: list komponen yang berhasil di-transition.
        """
        if component:
            ok = await async_transition_to_half_open(component)
            transitioned = [component] if ok else []
        else:
            transitioned = await async_resume_all_components()

        for comp in transitioned:
            await async_log_event(
                EventType.CIRCUIT_BREAKER_RESET,
                f"CB HALF_OPEN: [{comp}] via /resume",
                component=comp,
                severity=Severity.INFO,
            )
            logger.info("[cb] %s → HALF_OPEN via /resume", comp)

        return transitioned

    async def execute_with_cb(
        self,
        component: str,
        coro: Awaitable[T],
        *,
        is_position_monitor: bool = False,
    ) -> T:
        """
        Jalankan coroutine dengan circuit breaker protection.

        is_position_monitor=True:
            Bypass semua CB state check — monitoring posisi TIDAK pernah diblokir,
            sesuai spec: "Loop monitoring posisi TIDAK ikut berhenti saat trip".

        State handling:
            OPEN (& bukan monitor)  → raise CBOpenError tanpa menjalankan coro
            CLOSED / HALF_OPEN      → jalankan coro

        On CriticalError:
            CLOSED    → on_error (mungkin trip ke OPEN)
            HALF_OPEN → on_halfopen_failure (balik ke OPEN)

        On success:
            → on_success (HALF_OPEN → CLOSED, atau reset counter di CLOSED)
        """
        if not is_position_monitor:
            state = await self._get_state(component)
            if state == CBState.OPEN:
                raise CBOpenError(
                    f"Circuit breaker OPEN untuk '{component}' — "
                    "eksekusi ditolak. Kirim /resume setelah masalah diatasi."
                )

        is_half = (await self._get_state(component)) == CBState.HALF_OPEN

        try:
            result = await coro
        except CriticalError as exc:
            msg = str(exc)
            if is_half:
                await self.on_halfopen_failure(component, msg)
            else:
                await self.on_error(component, msg)
            raise
        except TransientError:
            # Transient seharusnya sudah di-retry oleh @with_retry dan
            # di-convert ke CriticalError jika semua retry habis.
            # Guard saja — tidak men-trip CB untuk sisa transient.
            raise
        else:
            await self.on_success(component)
            return result  # type: ignore[return-value]

    # ── Read-only helpers ──────────────────────────────────────────────────────

    def is_open_sync(self, component: str) -> bool:
        """Sync check (pakai saat tidak bisa await)."""
        return is_cb_open(component)

    async def is_open(self, component: str) -> bool:
        return await async_is_cb_open(component)

    async def get_summary(self) -> dict:
        """Ringkasan semua state — untuk /status command."""
        return await async_get_cb_summary_for_dashboard()

    # ── Internals ──────────────────────────────────────────────────────────────

    async def _get_state(self, component: str) -> str:
        rec = await async_get_cb_state(component)
        return rec["state"] if rec else CBState.CLOSED

    def _prune_window(self, component: str) -> None:
        """Hapus timestamp error yang sudah keluar dari sliding window."""
        cutoff = datetime.now(timezone.utc) - self._window
        dq = self._error_times[component]
        while dq and dq[0] < cutoff:
            dq.popleft()

    async def _trip(self, component: str, last_error: str) -> None:
        """Trip circuit breaker ke OPEN, log event, kirim alert Telegram."""
        await async_trip_circuit_breaker(component, last_error)
        self._error_times[component].clear()

        await async_log_event(
            EventType.CIRCUIT_BREAKER_TRIP,
            f"CB TRIPPED: [{component}] — {last_error[:200]}",
            component=component,
            severity=Severity.CRITICAL,
        )

        alert = (
            f"🚨 *CIRCUIT BREAKER TRIP* — `{component}`\n"
            f"\n"
            f"Komponen ini sekarang *OPEN* — eksekusi sinyal baru dihentikan.\n"
            f"Monitoring posisi yang sudah open tetap berjalan.\n"
            f"\n"
            f"Error: {last_error[:200]}\n"
            f"\n"
            f"Threshold: {self._threshold} error dalam "
            f"{int(self._window.total_seconds() / 60)} menit.\n"
            f"\n"
            f"⚠️ Kirim /resume setelah masalah diselesaikan."
        )
        logger.critical("[cb] 🚨 TRIPPED: %s | %s", component, last_error[:100])
        await self._notify(alert)

    async def _notify(self, text: str) -> None:
        if self._notify_fn is None:
            logger.debug("[cb] notify_fn belum diset — pesan tidak dikirim: %s", text[:60])
            return
        try:
            await self._notify_fn(text)
        except Exception as exc:
            logger.error("[cb] Gagal kirim notifikasi Telegram: %s", exc)


# ── Singleton ────────────────────────────────────────────────────────────────

_manager: Optional[CircuitBreakerManager] = None
_manager_lock = asyncio.Lock()


def get_circuit_breaker() -> CircuitBreakerManager:
    """
    Return singleton CircuitBreakerManager.
    Dibuat lazy — settings dibaca saat pertama kali dipanggil.

    Note: tidak butuh await — instance dibuat sync, asyncio lock
    hanya diperlukan jika dua coroutine pertama kali memanggil ini
    secara bersamaan (sangat jarang terjadi di startup sekuensial).
    """
    global _manager
    if _manager is None:
        from config.settings import settings

        _manager = CircuitBreakerManager(
            error_threshold=settings.CIRCUIT_BREAKER_ERROR_THRESHOLD,
            window_minutes=settings.CIRCUIT_BREAKER_WINDOW_MINUTES,
        )
    return _manager
