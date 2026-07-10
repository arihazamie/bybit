"""
exchange/bitget/market_data.py
================================
Loader & cache untuk market list Bitget Futures (USDT-M), dipakai signal
parser (Step 4) untuk validasi & normalisasi simbol pair dari sinyal.

Penting: market list Bitget Perp **tidak hanya crypto** — ada juga kontrak
perpetual untuk komoditas (mis. XAU/emas) dan beberapa saham AS, semua
di-settle dalam USDT. Modul ini query SELURUH market list (semua kategori
kontrak USDT-M), bukan cuma yang diasumsikan crypto.

Step 4 hanya butuh validasi *read-only* terhadap symbol list. Koneksi REST
penuh (fetch balance, set leverage, dll.) baru dibangun di Step 7 — modul ini
sengaja diisolasi & minimal supaya bisa dipakai mandiri oleh parser tanpa
bergantung ke modul-modul yang belum ada.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Optional

from core.logging_setup import get_logger

logger = get_logger(__name__)

# Refresh market list tiap 1 jam — market list Bitget tidak berubah tiap detik,
# tapi tetap di-refresh berkala supaya listing baru/delisting kebaca otomatis.
CACHE_TTL_SECONDS = 60 * 60

# Pair non-crypto yang diketahui beredar di Bitget Perp — dipakai HANYA untuk
# klasifikasi informasional di notifikasi/log, BUKAN untuk membatasi validasi.
# Validasi tetap berdasarkan keberadaan simbol di market list asli, bukan list ini.
_KNOWN_COMMODITY_BASES = {"XAU", "XAG"}


@dataclass
class MarketMatch:
    """Hasil pencarian pair di market list Bitget."""

    symbol: str    # unified ccxt symbol, mis. "STG/USDT:USDT"
    base: str      # base currency, mis. "STG"
    category: str  # "crypto" | "commodity" | "unknown" — informasional saja


class BitgetMarketCache:
    """
    Cache in-memory untuk seluruh market list Bitget Futures (USDT-M).

    Pemakaian:
        cache = BitgetMarketCache()
        match = await cache.find_symbol("XAU")   # -> MarketMatch atau None
    """

    def __init__(self, sandbox: Optional[bool] = None) -> None:
        self._markets: dict[str, dict] = {}
        self._base_index: dict[str, str] = {}  # BASE (upper) -> unified symbol
        self._last_loaded: float = 0.0
        self._lock = asyncio.Lock()
        self._sandbox = sandbox

    async def _load_if_stale(self) -> None:
        now = time.time()
        if self._markets and (now - self._last_loaded) < CACHE_TTL_SECONDS:
            return

        async with self._lock:
            # Cek ulang di dalam lock — double-checked locking, hindari
            # beberapa coroutine reload bersamaan.
            now = time.time()
            if self._markets and (now - self._last_loaded) < CACHE_TTL_SECONDS:
                return
            await self._load_markets()

    async def _load_markets(self) -> None:
        """Fetch seluruh market list Bitget via ccxt (semua kategori kontrak USDT-M)."""
        try:
            import ccxt.async_support as ccxt_async
        except ImportError as exc:
            raise RuntimeError(
                "Package 'ccxt' belum terinstall — jalankan: pip install ccxt"
            ) from exc

        from config.settings import settings

        exchange = ccxt_async.bitget({
            "apiKey": settings.BITGET_API_KEY,
            "secret": settings.BITGET_API_SECRET,
            "password": settings.BITGET_PASSPHRASE,
            "enableRateLimit": True,
        })

        use_sandbox = self._sandbox if self._sandbox is not None else settings.BITGET_USE_SANDBOX
        if use_sandbox:
            exchange.set_sandbox_mode(True)

        try:
            markets = await exchange.load_markets(reload=True)
        finally:
            await exchange.close()

        # Filter: kontrak swap/futures yang di-settle USDT. Ini sudah mencakup
        # SEMUA kategori (crypto, komoditas, saham tokenized) karena produk
        # USDT-M Bitget Perp menyamaratakan settle currency-nya ke USDT,
        # apapun underlying asset-nya — jadi TIDAK ada asumsi crypto-only di sini.
        usdt_markets = {
            sym: m for sym, m in markets.items()
            if m.get("swap") and m.get("settle") == "USDT" and m.get("active", True)
        }

        base_index: dict[str, str] = {}
        for sym, m in usdt_markets.items():
            base = (m.get("base") or "").upper()
            if base:
                base_index[base] = sym

        self._markets = usdt_markets
        self._base_index = base_index
        self._last_loaded = time.time()
        logger.info(
            "[market_data] Market list Bitget Futures dimuat — %d simbol USDT-M tersedia",
            len(usdt_markets),
        )

    async def find_symbol(self, pair_raw: str) -> Optional[MarketMatch]:
        """
        Cari unified symbol Bitget untuk pair mentah dari sinyal (mis. "STG", "$XAU").

        Returns:
            MarketMatch jika simbol ada di market list Bitget, None jika tidak ketemu
            (artinya sinyal harus dianggap ambigu — pair tidak dikenali).
        """
        await self._load_if_stale()

        base = pair_raw.strip().upper().lstrip("$")
        symbol = self._base_index.get(base)
        if symbol is None:
            return None

        category = "commodity" if base in _KNOWN_COMMODITY_BASES else "crypto"
        return MarketMatch(symbol=symbol, base=base, category=category)


# ── Singleton default ─────────────────────────────────────────────────────
# Dipakai signal parser secara default di production. Untuk unit testing,
# suntikkan validator/cache palsu — JANGAN andalkan singleton ini (butuh
# koneksi network asli ke Bitget).
_default_cache: Optional[BitgetMarketCache] = None


def get_default_market_cache() -> BitgetMarketCache:
    global _default_cache
    if _default_cache is None:
        _default_cache = BitgetMarketCache()
    return _default_cache
