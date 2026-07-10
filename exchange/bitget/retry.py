"""
exchange/bitget/retry.py
========================
Utilitas retry + klasifikasi error untuk semua operasi Bitget REST.

Klasifikasi error:
  - Transient  : timeout, rate limit (429), API down sementara, koneksi websocket putus
                 → retry otomatis dengan exponential backoff (2s, 5s, 15s)
  - Critical   : API key invalid, permission ditolak, order ditolak karena alasan bisnis
                 → TIDAK retry, langsung trip circuit breaker

Sesuai spesifikasi Section 10.1 prompt.md.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
from typing import Any, Callable, Optional, Tuple, Type

import ccxt

from core.constants import RETRY_BACKOFF_SECONDS
from core.logging_setup import get_logger

logger = get_logger(__name__)


# ── Custom exception types ───────────────────────────────────────────────────

class TransientError(Exception):
    """
    Error sementara yang boleh di-retry otomatis.
    Contoh: timeout jaringan, rate limit (HTTP 429), API down sementara.
    """
    def __init__(self, message: str, original: Optional[Exception] = None):
        super().__init__(message)
        self.original = original


class CriticalError(Exception):
    """
    Error kritis yang TIDAK boleh di-retry — harus trip circuit breaker.
    Contoh: API key invalid, permission ditolak, simbol di-suspend.
    """
    def __init__(self, message: str, original: Optional[Exception] = None):
        super().__init__(message)
        self.original = original


# ── Error classification ─────────────────────────────────────────────────────

# Exception ccxt yang dianggap TRANSIENT (boleh retry)
_TRANSIENT_CCXT_TYPES: Tuple[Type[Exception], ...] = (
    ccxt.NetworkError,         # generic network failure
    ccxt.RequestTimeout,       # request timeout
    ccxt.DDoSProtection,       # rate limit / DDoS protection (HTTP 429 / 418)
    ccxt.ExchangeNotAvailable, # exchange maintenance / temporary unavailable
    ccxt.InvalidNonce,         # nonce drift — biasanya transient setelah restart
)

# Exception ccxt yang dianggap CRITICAL (jangan retry, trip circuit breaker)
_CRITICAL_CCXT_TYPES: Tuple[Type[Exception], ...] = (
    ccxt.AuthenticationError,  # API key invalid / expired / permission kurang
    ccxt.PermissionDenied,     # endpoint tidak diizinkan oleh API key
    ccxt.AccountSuspended,     # akun kena suspend
    ccxt.BadSymbol,            # simbol tidak valid di exchange (permanent)
    ccxt.BadRequest,           # request malformed — bug di kode kita, bukan transient
    ccxt.InsufficientFunds,    # saldo tidak cukup — bukan transient, harus notifikasi
    ccxt.InvalidOrder,         # parameter order tidak valid
    ccxt.OrderNotFound,        # order tidak ditemukan
)


def classify_exception(exc: Exception) -> str:
    """
    Klasifikasikan exception ccxt sebagai 'transient' atau 'critical'.

    Return:
        'transient' — boleh retry
        'critical'  — jangan retry, trip circuit breaker
        'unknown'   — exception non-ccxt, fallback ke critical
    """
    if isinstance(exc, _TRANSIENT_CCXT_TYPES):
        return "transient"

    if isinstance(exc, _CRITICAL_CCXT_TYPES):
        return "critical"

    # ccxt.ExchangeError adalah base class — cek pesan untuk kasus edge
    if isinstance(exc, ccxt.ExchangeError):
        msg = str(exc).lower()
        # Beberapa exchange mengembalikan "maintenance" atau "unavailable" sebagai ExchangeError
        if any(kw in msg for kw in ("maintenance", "unavailable", "temporarily", "rate limit")):
            return "transient"
        # Default ExchangeError yang tidak dikenali → critical (aman lebih konservatif)
        return "critical"

    # Non-ccxt exception (mis. ConnectionError, asyncio.TimeoutError)
    if isinstance(exc, (ConnectionError, TimeoutError, asyncio.TimeoutError)):
        return "transient"

    # Semua yang tidak dikenali → critical untuk keamanan
    return "critical"


# ── Retry decorator ──────────────────────────────────────────────────────────

def with_retry(
    backoff: Tuple[float, ...] = RETRY_BACKOFF_SECONDS,
    reraise_as_critical: bool = True,
) -> Callable:
    """
    Decorator untuk async function — retry otomatis jika terjadi TransientError
    atau exception ccxt transient.

    Args:
        backoff             : tuple detik jeda antar percobaan (default dari constants.py)
        reraise_as_critical : jika True, setelah semua retry habis raise CriticalError;
                              jika False, raise TransientError aslinya

    Contoh pemakaian:
        @with_retry()
        async def fetch_balance(self):
            ...

        @with_retry(backoff=(1, 3, 10))
        async def set_margin_mode(self, symbol):
            ...
    """
    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            last_exc: Optional[Exception] = None
            attempts = len(backoff) + 1  # total percobaan = jumlah jeda + 1

            for attempt in range(1, attempts + 1):
                try:
                    return await fn(*args, **kwargs)

                except (TransientError, *_TRANSIENT_CCXT_TYPES, ConnectionError,
                        TimeoutError, asyncio.TimeoutError) as exc:
                    last_exc = exc
                    kind = "transient"

                except CriticalError:
                    # Critical error sudah dibungkus — langsung re-raise
                    raise

                except _CRITICAL_CCXT_TYPES as exc:
                    # Bungkus ke CriticalError dan raise tanpa retry
                    raise CriticalError(
                        f"Critical error di {fn.__name__}: {exc}",
                        original=exc,
                    ) from exc

                except Exception as exc:
                    # Exception tidak dikenal — klasifikasikan dulu
                    kind = classify_exception(exc)
                    if kind == "transient":
                        last_exc = exc
                    else:
                        raise CriticalError(
                            f"Critical error di {fn.__name__}: {exc}",
                            original=exc,
                        ) from exc

                # Sampai di sini → transient error, coba lagi jika masih ada jeda
                if attempt <= len(backoff):
                    wait = backoff[attempt - 1]
                    logger.warning(
                        "[retry] %s — percobaan %d/%d gagal (%s). "
                        "Retry dalam %.0f detik...",
                        fn.__name__, attempt, attempts, last_exc, wait,
                    )
                    await asyncio.sleep(wait)
                else:
                    # Semua percobaan habis
                    logger.error(
                        "[retry] %s — semua %d percobaan gagal. Error terakhir: %s",
                        fn.__name__, attempts, last_exc,
                    )
                    if reraise_as_critical:
                        raise CriticalError(
                            f"Semua {attempts} percobaan {fn.__name__} gagal: {last_exc}",
                            original=last_exc,
                        ) from last_exc
                    raise last_exc  # type: ignore[misc]

            # Seharusnya tidak sampai sini
            raise RuntimeError(f"with_retry: loop selesai tanpa return — {fn.__name__}")

        return wrapper
    return decorator


# ── Convenience: classify & wrap raw exception ───────────────────────────────

def wrap_exchange_error(exc: Exception, context: str = "") -> Exception:
    """
    Bungkus exception mentah dari ccxt ke TransientError atau CriticalError.
    Berguna untuk blok try/except manual (tanpa decorator).

    Args:
        exc     : exception asli dari ccxt / network
        context : string deskripsi operasi (untuk pesan error yang informatif)

    Return:
        TransientError atau CriticalError — siap di-raise
    """
    prefix = f"[{context}] " if context else ""
    kind = classify_exception(exc)

    if kind == "transient":
        return TransientError(f"{prefix}Transient error: {exc}", original=exc)
    return CriticalError(f"{prefix}Critical error: {exc}", original=exc)
