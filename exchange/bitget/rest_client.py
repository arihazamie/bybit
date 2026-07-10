"""
exchange/bitget/rest_client.py
==============================
Bitget Futures REST client — koneksi, autentikasi, dan operasi dasar.

Tanggung jawab modul ini (Step 7):
  1. Koneksi ccxt.bitget (REST) + autentikasi API key
  2. Fetch balance — total equity & free margin (USDT-M cross account)
  3. Fetch market list lengkap semua kategori (crypto, komoditas, saham)
  4. Set margin mode CROSS per simbol — WAJIB sebelum setiap entry
  5. Fetch max leverage per simbol secara dinamis — JANGAN hardcode
  6. Health check / ping ke exchange

Semua fungsi:
  - Pakai try/except + retry logic via @with_retry decorator
  - Raise CriticalError untuk error permanen (auth, permission, dll.)
  - Raise TransientError untuk error sementara (timeout, rate limit, dll.)
  - Log setiap operasi penting dengan context yang cukup untuk debugging

PENTING: Modul ini hanya REST. WebSocket (watch_orders, watch_positions)
ada di exchange/bitget/ws_client.py (Step 8) — ws_client memakai
fetch_open_orders() dan fetch_positions() di bawah sebagai fallback
reconciliation periodik.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import ccxt
import ccxt.async_support as ccxt_async

from config.settings import settings
from core.logging_setup import get_logger
from exchange.bitget.retry import CriticalError, TransientError, with_retry

logger = get_logger(__name__)


# ── Data containers ──────────────────────────────────────────────────────────

@dataclass
class BalanceInfo:
    """
    Snapshot balance akun Bitget Futures (USDT-M, mode Cross).

    Catatan terminologi:
      - total_equity   : total equity akun = wallet balance + unrealized PnL semua posisi
                         INI yang dipakai untuk kalkulasi risk_amount di mode Percent
      - free_margin    : margin yang masih tersedia untuk buka posisi baru
                         INI yang divalidasi sebelum eksekusi (margin_needed <= free_margin)
      - wallet_balance : saldo murni tanpa unrealized PnL
      - unrealized_pnl : total unrealized PnL dari semua posisi open
    """
    total_equity: float          # equity akun (saldo + unrealized PnL)
    free_margin: float           # margin tersedia untuk posisi baru
    wallet_balance: float        # saldo murni
    unrealized_pnl: float        # unrealized PnL total
    snapshot_at: float = field(default_factory=time.time)   # unix timestamp

    def __str__(self) -> str:
        return (
            f"BalanceInfo("
            f"equity={self.total_equity:.4f} USDT, "
            f"free={self.free_margin:.4f} USDT, "
            f"wallet={self.wallet_balance:.4f} USDT, "
            f"upnl={self.unrealized_pnl:+.4f} USDT"
            f")"
        )


@dataclass
class MarketInfo:
    """
    Informasi market untuk satu simbol futures (hasil dari fetch_all_markets).
    """
    symbol: str            # unified ccxt symbol, mis. "BTC/USDT:USDT"
    base: str              # base currency, mis. "BTC"
    quote: str             # quote currency, selalu "USDT"
    settle: str            # settle currency, selalu "USDT"
    max_leverage: float    # max leverage yang tersedia untuk simbol ini
    min_leverage: float    # biasanya 1.0
    contract_size: float   # ukuran 1 kontrak (dalam base currency)
    active: bool           # apakah market aktif / bisa ditradingkan
    raw: Dict[str, Any] = field(default_factory=dict)  # raw ccxt market dict


# ── Helpers ──────────────────────────────────────────────────────────────────

def _safe_float(value: Any, default: float = 0.0) -> float:
    """
    Konversi value ke float dengan aman — return default jika None, kosong, atau tidak valid.
    Dipakai di _parse_balance untuk menghindari ValueError saat field Bitget API kosong/None.
    """
    if value is None or value == "" or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# ── BitgetRestClient ─────────────────────────────────────────────────────────

class BitgetRestClient:
    """
    REST client untuk Bitget Futures (USDT-M, mode Cross).

    Lazy initialization — exchange object dibuat saat pertama kali dibutuhkan,
    bukan saat __init__. Ini memungkinkan client dibuat tanpa langsung
    melakukan network call.

    Pemakaian:
        client = BitgetRestClient()
        balance = await client.fetch_balance()
        leverage = await client.get_max_leverage("BTC/USDT:USDT")
        await client.set_cross_margin("BTC/USDT:USDT")
        await client.close()

    Context manager:
        async with BitgetRestClient() as client:
            balance = await client.fetch_balance()
    """

    # Cache market list — shared di semua instance agar tidak reload tiap waktu
    _market_cache: Dict[str, MarketInfo] = {}
    _market_cache_loaded_at: float = 0.0
    _MARKET_CACHE_TTL = 3600.0   # 1 jam

    def __init__(
        self,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        passphrase: Optional[str] = None,
        sandbox: Optional[bool] = None,
    ) -> None:
        """
        Args:
            api_key, api_secret, passphrase : override credentials (default dari settings)
            sandbox : override sandbox mode (default dari settings.BITGET_USE_SANDBOX)
        """
        self._api_key = api_key or settings.BITGET_API_KEY
        self._api_secret = api_secret or settings.BITGET_API_SECRET
        self._passphrase = passphrase or settings.BITGET_PASSPHRASE
        self._sandbox = sandbox if sandbox is not None else settings.BITGET_USE_SANDBOX

        self._exchange: Optional[ccxt_async.bitget] = None
        self._lock = asyncio.Lock()

    # ── Lifecycle ────────────────────────────────────────────────────────

    async def _get_exchange(self) -> ccxt_async.bitget:
        """
        Lazy-init: buat exchange object jika belum ada.
        Thread-safe via asyncio.Lock.
        """
        if self._exchange is not None:
            return self._exchange

        async with self._lock:
            if self._exchange is not None:
                return self._exchange

            exchange = ccxt_async.bitget({
                "apiKey": self._api_key,
                "secret": self._api_secret,
                "password": self._passphrase,
                "enableRateLimit": True,
                "options": {
                    # Gunakan V2 API Bitget jika tersedia di ccxt
                    "defaultType": "swap",          # default ke futures/swap endpoint
                    "fetchTickerQuotes": False,
                },
            })

            if self._sandbox:
                exchange.set_sandbox_mode(True)
                logger.info("[rest_client] Sandbox mode AKTIF — tidak ada order real")

            self._exchange = exchange
            logger.info(
                "[rest_client] BitgetRestClient initialized (sandbox=%s)", self._sandbox
            )

        return self._exchange

    async def close(self) -> None:
        """Tutup koneksi exchange. Wajib dipanggil saat shutdown."""
        if self._exchange is not None:
            try:
                await self._exchange.close()
            except Exception as exc:
                logger.warning("[rest_client] Error saat close exchange: %s", exc)
            finally:
                self._exchange = None
                logger.debug("[rest_client] Exchange connection closed")

    async def __aenter__(self) -> "BitgetRestClient":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    # ── Health check ─────────────────────────────────────────────────────

    @with_retry()
    async def ping(self) -> bool:
        """
        Health check: fetch balance kecil untuk verifikasi koneksi & auth.
        Return True jika berhasil, raise CriticalError/TransientError jika gagal.

        Dipakai oleh circuit breaker (state HALF_OPEN → test satu operasi kecil).
        """
        try:
            exchange = await self._get_exchange()
            # fetch_balance adalah operasi paling ringan yang butuh auth
            raw = await exchange.fetch_balance({"type": "swap"})
            logger.debug("[rest_client] ping OK — exchange reachable")
            return True
        except (CriticalError, TransientError):
            raise
        except ccxt.AuthenticationError as exc:
            raise CriticalError(
                f"[ping] Autentikasi gagal — cek API key/secret/passphrase: {exc}",
                original=exc,
            ) from exc
        except ccxt.NetworkError as exc:
            raise TransientError(f"[ping] Network error: {exc}", original=exc) from exc
        except Exception as exc:
            raise CriticalError(f"[ping] Unexpected error: {exc}", original=exc) from exc

    # ── Balance ──────────────────────────────────────────────────────────

    @with_retry()
    async def fetch_balance(self) -> BalanceInfo:
        """
        Fetch balance akun Bitget Futures (USDT-M, mode Cross).

        Return:
            BalanceInfo dengan total_equity, free_margin, wallet_balance, unrealized_pnl

        Catatan implementasi:
            Bitget menyimpan balance futures di endpoint swap. ccxt meng-expose via
            fetch_balance({'type': 'swap'}). Nilai equity (termasuk unrealized PnL)
            biasanya ada di raw info response — kita cek dua jalur:
              1. ccxt standard: balance['USDT'] dict
              2. Bitget raw: balance['info']['data'][*]['usdtEquity'] (V1/V2)
        """
        try:
            exchange = await self._get_exchange()
            raw = await exchange.fetch_balance({"type": "swap"})
            return self._parse_balance(raw)

        except (CriticalError, TransientError):
            raise
        except ccxt.AuthenticationError as exc:
            raise CriticalError(
                f"[fetch_balance] Auth gagal: {exc}", original=exc
            ) from exc
        except ccxt.NetworkError as exc:
            raise TransientError(
                f"[fetch_balance] Network error: {exc}", original=exc
            ) from exc
        except Exception as exc:
            raise CriticalError(
                f"[fetch_balance] Unexpected error: {exc}", original=exc
            ) from exc

    def _parse_balance(self, raw: Dict[str, Any]) -> BalanceInfo:
        """
        Parse raw ccxt balance response ke BalanceInfo.
        Mendukung 3 jalur secara berurutan (paling akurat duluan):

          1. Raw Bitget API response di raw['info']['data'] — paling lengkap,
             mengandung usdtEquity (wallet + unrealized PnL), available margin, dsb.
          2. ccxt standard balance['USDT'] — fallback kalau raw info tidak tersedia
          3. Default 0.0 — jika keduanya tidak mengandung data valid (mis. akun baru)

        Variabel naming:
          raw_info  — dict mentah dari raw['info'] (Bitget API response)
          acct      — satu account object dari data_list
          result    — BalanceInfo yang akan di-return (bukan 'info' untuk hindari konflik)
        """
        # ── Jalur 1: raw Bitget API (paling akurat, ada unrealized PnL) ────
        equity = 0.0
        free   = 0.0
        wallet = 0.0
        upnl   = 0.0
        parsed_from_raw = False

        raw_info = raw.get("info") or {}

        # Format Bitget V2: {'code': '00000', 'data': [{...}], 'msg': 'success'}
        # Format Bitget V1: bisa langsung dict atau {'data': {...}}
        data_list = raw_info.get("data") or []

        if isinstance(data_list, list) and data_list:
            # V2 format — ambil account USDT-M pertama dari list
            acct = data_list[0]
            equity = _safe_float(acct.get("usdtEquity")   or acct.get("equity"))
            free   = _safe_float(acct.get("available")    or acct.get("maxOpenPosAvailable"))
            wallet = _safe_float(acct.get("accountEquity") or acct.get("fixedMaxAvailable"))
            upnl   = _safe_float(acct.get("unrealizedPL") or acct.get("unrealizedProfit"))
            parsed_from_raw = equity > 0 or free > 0

        elif isinstance(data_list, dict) and data_list:
            # Beberapa versi ccxt/Bitget return dict langsung (bukan list)
            acct = data_list
            equity = _safe_float(acct.get("usdtEquity")    or acct.get("equity"))
            free   = _safe_float(acct.get("available")     or acct.get("maxOpenPosAvailable"))
            wallet = _safe_float(acct.get("accountEquity") or acct.get("fixedMaxAvailable"))
            upnl   = _safe_float(acct.get("unrealizedPL")  or acct.get("unrealizedProfit"))
            parsed_from_raw = equity > 0 or free > 0

        # ── Jalur 2: ccxt standard balance['USDT'] ──────────────────────────
        # Pakai sebagai primary jika raw tidak yield data valid,
        # atau sebagai cross-check / fallback partial field
        usdt = raw.get("USDT") or {}
        total_ccxt = _safe_float(usdt.get("total"))
        free_ccxt  = _safe_float(usdt.get("free"))

        if not parsed_from_raw:
            # Raw info kosong atau tidak ada data — pakai ccxt standard
            equity = total_ccxt
            free   = free_ccxt
            wallet = total_ccxt   # tanpa unrealized PnL — terbaik yang bisa dari ccxt standard
            upnl   = 0.0
            logger.debug("[rest_client] Balance parsed dari ccxt standard (raw info tidak tersedia)")
        else:
            # Raw berhasil — tapi jika ada field yang masih 0 dan ccxt punya nilai, pakai ccxt
            if equity <= 0 and total_ccxt > 0:
                equity = total_ccxt
            if free <= 0 and free_ccxt > 0:
                free = free_ccxt
            if wallet <= 0 and total_ccxt > 0:
                wallet = total_ccxt
            logger.debug("[rest_client] Balance parsed dari raw Bitget API")

        result = BalanceInfo(
            total_equity=equity,
            free_margin=free,
            wallet_balance=wallet,
            unrealized_pnl=upnl,
        )
        logger.debug("[rest_client] %s", result)
        return result

    # ── Market list ──────────────────────────────────────────────────────

    @with_retry()
    async def fetch_all_markets(self, force_reload: bool = False) -> Dict[str, MarketInfo]:
        """
        Fetch seluruh market list Bitget Futures USDT-M (semua kategori:
        crypto, komoditas, saham tokenized).

        Return:
            dict mapping unified_symbol -> MarketInfo

        Cache TTL 1 jam — panggil dengan force_reload=True untuk force refresh.
        """
        now = time.time()
        if (
            not force_reload
            and BitgetRestClient._market_cache
            and (now - BitgetRestClient._market_cache_loaded_at) < self._MARKET_CACHE_TTL
        ):
            logger.debug(
                "[rest_client] Pakai market cache (%d symbols)",
                len(BitgetRestClient._market_cache),
            )
            return BitgetRestClient._market_cache

        try:
            exchange = await self._get_exchange()
            raw_markets = await exchange.load_markets(reload=True)
            markets = self._parse_markets(raw_markets)

            # Update class-level cache
            BitgetRestClient._market_cache = markets
            BitgetRestClient._market_cache_loaded_at = time.time()

            logger.info(
                "[rest_client] Market list dimuat — %d simbol USDT-M futures tersedia",
                len(markets),
            )
            return markets

        except (CriticalError, TransientError):
            raise
        except ccxt.NetworkError as exc:
            raise TransientError(
                f"[fetch_all_markets] Network error: {exc}", original=exc
            ) from exc
        except Exception as exc:
            raise CriticalError(
                f"[fetch_all_markets] Unexpected error: {exc}", original=exc
            ) from exc

    def _parse_markets(self, raw_markets: Dict[str, Any]) -> Dict[str, MarketInfo]:
        """
        Filter & parse raw ccxt market dict ke {symbol: MarketInfo}.

        Filter: hanya USDT-M swap/futures (settle=USDT, swap=True).
        Ini otomatis mencakup SEMUA kategori (crypto, komoditas, saham)
        tanpa harus filter per-category secara manual.
        """
        result: Dict[str, MarketInfo] = {}

        for sym, m in raw_markets.items():
            # Hanya kontrak swap (perpetual futures) yang di-settle USDT
            if not (m.get("swap") and m.get("settle") == "USDT"):
                continue
            if not m.get("active", True):
                continue

            # Ambil leverage — bisa ada di beberapa lokasi di ccxt
            limits = m.get("limits") or {}
            lev_limits = limits.get("leverage") or {}
            max_lev = float(lev_limits.get("max") or lev_limits.get("maximum") or 1.0)
            min_lev = float(lev_limits.get("min") or lev_limits.get("minimum") or 1.0)

            # Jika ccxt tidak expose leverage, fallback ke info Bitget raw
            if max_lev <= 1.0:
                info = m.get("info") or {}
                max_lev = float(
                    info.get("maxLeverage") or
                    info.get("maxOpenLeverage") or
                    info.get("supportMaintenanceMarginRate") or
                    1.0
                )

            result[sym] = MarketInfo(
                symbol=sym,
                base=m.get("base", ""),
                quote=m.get("quote", "USDT"),
                settle=m.get("settle", "USDT"),
                max_leverage=max_lev,
                min_leverage=max(1.0, min_lev),
                contract_size=float(m.get("contractSize") or 1.0),
                active=bool(m.get("active", True)),
                raw=m,
            )

        return result

    # ── Leverage ─────────────────────────────────────────────────────────

    @with_retry()
    async def get_max_leverage(self, symbol: str) -> float:
        """
        Fetch max leverage untuk satu simbol secara dinamis dari Bitget.

        WAJIB dipanggil sebelum setiap entry — JANGAN hardcode angka leverage.
        Setiap pair bisa punya max leverage berbeda (BTC mungkin 125x,
        altcoin kecil mungkin hanya 20x).

        Return:
            Max leverage sebagai float (mis. 125.0, 20.0, 10.0)

        Raises:
            CriticalError jika simbol tidak ditemukan di market list
        """
        # 1. Cek dari market cache dulu (paling cepat, tidak ada network call)
        markets = await self.fetch_all_markets()
        if symbol in markets:
            lev = markets[symbol].max_leverage
            if lev > 1.0:
                logger.debug(
                    "[rest_client] Max leverage %s: %.0fx (dari cache)",
                    symbol, lev,
                )
                return lev

        # 2. Cache tidak punya atau max_lev <= 1 → query fresh dari exchange
        try:
            exchange = await self._get_exchange()

            # Coba fetch_leverage_tiers (tersedia di beberapa exchange via ccxt)
            try:
                tiers = await exchange.fetch_leverage_tiers([symbol])
                if tiers and symbol in tiers:
                    tier_list = tiers[symbol]
                    if tier_list:
                        # Tier pertama biasanya punya leverage tertinggi
                        max_lev = float(
                            tier_list[0].get("maxLeverage") or
                            tier_list[0].get("leverage") or
                            1.0
                        )
                        if max_lev > 1.0:
                            logger.info(
                                "[rest_client] Max leverage %s: %.0fx (via fetch_leverage_tiers)",
                                symbol, max_lev,
                            )
                            return max_lev
            except (ccxt.NotSupported, ccxt.ExchangeError, AttributeError):
                # fetch_leverage_tiers tidak didukung → fallback ke market info
                pass

            # 3. Fallback: reload market list dan ambil dari sana
            markets = await self.fetch_all_markets(force_reload=True)
            if symbol in markets:
                lev = markets[symbol].max_leverage
                logger.info(
                    "[rest_client] Max leverage %s: %.0fx (via market reload)",
                    symbol, lev,
                )
                return max(1.0, lev)

            # 4. Simbol tidak ditemukan sama sekali
            raise CriticalError(
                f"[get_max_leverage] Simbol '{symbol}' tidak ditemukan di market list Bitget. "
                f"Pastikan simbol valid dan menggunakan format 'BASE/USDT:USDT'."
            )

        except (CriticalError, TransientError):
            raise
        except ccxt.BadSymbol as exc:
            raise CriticalError(
                f"[get_max_leverage] Simbol tidak valid '{symbol}': {exc}", original=exc
            ) from exc
        except ccxt.NetworkError as exc:
            raise TransientError(
                f"[get_max_leverage] Network error untuk '{symbol}': {exc}", original=exc
            ) from exc
        except Exception as exc:
            raise CriticalError(
                f"[get_max_leverage] Unexpected error untuk '{symbol}': {exc}", original=exc
            ) from exc

    @with_retry()
    async def set_leverage(self, symbol: str, leverage: int) -> None:
        """
        Set leverage untuk simbol tertentu.

        Dipanggil setelah get_max_leverage() + safety check (Step 10).
        Bitget mengharuskan set leverage SEBELUM buka posisi.

        Args:
            symbol   : unified ccxt symbol (mis. "BTC/USDT:USDT")
            leverage : angka leverage yang sudah divalidasi (integer)

        Raises:
            CriticalError jika leverage tidak valid atau ditolak exchange
        """
        try:
            exchange = await self._get_exchange()
            await exchange.set_leverage(leverage, symbol)
            logger.info(
                "[rest_client] Leverage set: %s → %dx", symbol, leverage
            )
        except (CriticalError, TransientError):
            raise
        except ccxt.BadRequest as exc:
            raise CriticalError(
                f"[set_leverage] Leverage {leverage}x tidak valid untuk '{symbol}': {exc}",
                original=exc,
            ) from exc
        except ccxt.NetworkError as exc:
            raise TransientError(
                f"[set_leverage] Network error: {exc}", original=exc
            ) from exc
        except Exception as exc:
            raise CriticalError(
                f"[set_leverage] Unexpected error untuk '{symbol}': {exc}", original=exc
            ) from exc

    # ── Margin mode ──────────────────────────────────────────────────────

    @with_retry()
    async def set_cross_margin(self, symbol: str) -> None:
        """
        Set margin mode ke CROSS untuk simbol tertentu.

        WAJIB dipanggil sebelum setiap entry. Jangan asumsikan default akun
        sudah cross — eksplisit set supaya tidak ada posisi yang tidak sengaja
        masuk mode isolated.

        Args:
            symbol : unified ccxt symbol (mis. "BTC/USDT:USDT")

        Catatan:
            Beberapa exchange tidak support set_margin_mode via ccxt unified —
            kalau begitu kita log warning tapi tidak raise error (karena
            mungkin sudah cross dari akun setting).
        """
        try:
            exchange = await self._get_exchange()
            await exchange.set_margin_mode("cross", symbol)
            logger.info(
                "[rest_client] Margin mode CROSS set: %s ✓", symbol
            )
        except ccxt.NotSupported:
            # ccxt belum implement set_margin_mode untuk Bitget — log warning
            logger.warning(
                "[rest_client] set_margin_mode tidak didukung via ccxt untuk %s. "
                "Pastikan margin mode CROSS sudah diset manual di akun Bitget.",
                symbol,
            )
        except (CriticalError, TransientError):
            raise
        except ccxt.AuthenticationError as exc:
            raise CriticalError(
                f"[set_cross_margin] Auth error untuk '{symbol}': {exc}", original=exc
            ) from exc
        except ccxt.NetworkError as exc:
            raise TransientError(
                f"[set_cross_margin] Network error untuk '{symbol}': {exc}", original=exc
            ) from exc
        except Exception as exc:
            # Beberapa exchange raise generic error kalau mode sudah sama → ignore
            msg = str(exc).lower()
            if "same margin mode" in msg or "already" in msg or "no change" in msg:
                logger.debug(
                    "[rest_client] Margin mode untuk %s sudah CROSS (no-op)", symbol
                )
            else:
                raise CriticalError(
                    f"[set_cross_margin] Unexpected error untuk '{symbol}': {exc}",
                    original=exc,
                ) from exc

    # ── Open orders & positions (REST) ──────────────────────────────────
    # Dipakai sebagai fallback reconciliation oleh ws_client.py (Step 8) dan
    # oleh position_checker (Step 11) untuk cek kondisi pair sebelum eksekusi.

    @with_retry()
    async def fetch_open_orders(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Fetch semua open order (REST) untuk satu simbol, atau semua simbol jika
        `symbol=None`.

        Return: list raw ccxt order dict (unified format).
        """
        try:
            exchange = await self._get_exchange()
            params: Dict[str, Any] = {"type": "swap"}
            orders = await exchange.fetch_open_orders(symbol, params=params)
            return orders or []
        except (CriticalError, TransientError):
            raise
        except ccxt.NetworkError as exc:
            raise TransientError(
                f"[fetch_open_orders] Network error: {exc}", original=exc
            ) from exc
        except Exception as exc:
            raise CriticalError(
                f"[fetch_open_orders] Unexpected error: {exc}", original=exc
            ) from exc

    @with_retry()
    async def fetch_positions(
        self, symbols: Optional[List[str]] = None
    ) -> List[Dict[str, Any]]:
        """
        Fetch semua posisi (REST) — semua simbol USDT-M jika `symbols=None`.

        Return: list raw ccxt position dict (unified format). Posisi dengan
        `contracts=0` (sudah closed) tetap bisa muncul tergantung exchange —
        caller bertanggung jawab filter sesuai kebutuhan.
        """
        try:
            exchange = await self._get_exchange()
            positions = await exchange.fetch_positions(symbols, params={"type": "swap"})
            return positions or []
        except (CriticalError, TransientError):
            raise
        except ccxt.NetworkError as exc:
            raise TransientError(
                f"[fetch_positions] Network error: {exc}", original=exc
            ) from exc
        except Exception as exc:
            raise CriticalError(
                f"[fetch_positions] Unexpected error: {exc}", original=exc
            ) from exc

    # ── Ticker / harga pasar ────────────────────────────────────────────
    # Dipakai oleh risk_engine (Step 9) untuk estimasi entry_price saat
    # sinyal "Entry market" datang TANPA harga eksplisit — perhitungan
    # sl_distance/position_size butuh angka harga konkret, bukan "market".

    @with_retry()
    async def fetch_ticker_price(self, symbol: str) -> float:
        """
        Fetch harga terkini (last price) untuk satu simbol via REST ticker.

        HANYA dipakai untuk estimasi — bukan untuk eksekusi order itu sendiri
        (order market tetap dikirim sebagai order type 'market', exchange yang
        menentukan harga fill aktual). Risk engine memakai nilai ini semata
        untuk menghitung sl_distance/position_size SEBELUM order dikirim.

        Return:
            Harga last trade sebagai float. Fallback ke mid-price (bid+ask)/2
            jika 'last' tidak tersedia di response ticker.

        Raises:
            CriticalError jika simbol tidak valid atau ticker tidak punya
            harga yang bisa dipakai sama sekali.
        """
        try:
            exchange = await self._get_exchange()
            ticker = await exchange.fetch_ticker(symbol, params={"type": "swap"})

            price = _safe_float(ticker.get("last"))
            if price <= 0:
                bid = _safe_float(ticker.get("bid"))
                ask = _safe_float(ticker.get("ask"))
                if bid > 0 and ask > 0:
                    price = (bid + ask) / 2
                else:
                    price = _safe_float(ticker.get("close"))

            if price <= 0:
                raise CriticalError(
                    f"[fetch_ticker_price] Ticker '{symbol}' tidak punya harga "
                    f"valid (last/bid/ask/close semua kosong atau 0)."
                )

            logger.debug("[rest_client] Ticker price %s: %.8f", symbol, price)
            return price

        except (CriticalError, TransientError):
            raise
        except ccxt.BadSymbol as exc:
            raise CriticalError(
                f"[fetch_ticker_price] Simbol tidak valid '{symbol}': {exc}", original=exc
            ) from exc
        except ccxt.NetworkError as exc:
            raise TransientError(
                f"[fetch_ticker_price] Network error untuk '{symbol}': {exc}", original=exc
            ) from exc
        except Exception as exc:
            raise CriticalError(
                f"[fetch_ticker_price] Unexpected error untuk '{symbol}': {exc}", original=exc
            ) from exc

    # ── Market info helpers ───────────────────────────────────────────────

    async def get_market_info(self, symbol: str) -> Optional[MarketInfo]:
        """
        Ambil MarketInfo untuk satu simbol dari cache.
        Jika tidak ada di cache, trigger reload.

        Return None jika simbol tidak ditemukan di Bitget (pair tidak valid).
        """
        markets = await self.fetch_all_markets()
        if symbol in markets:
            return markets[symbol]

        # Coba reload sekali lagi
        markets = await self.fetch_all_markets(force_reload=True)
        return markets.get(symbol)

    async def symbol_exists(self, symbol: str) -> bool:
        """Cek apakah simbol terdaftar di Bitget Futures USDT-M."""
        return (await self.get_market_info(symbol)) is not None

    async def find_symbol_by_base(self, base: str) -> Optional[str]:
        """
        Cari unified symbol dari base currency (mis. "BTC" → "BTC/USDT:USDT").

        Berguna untuk normalisasi simbol dari sinyal (yang kadang hanya tulis "BTC").
        Return None jika tidak ditemukan.
        """
        markets = await self.fetch_all_markets()
        base_upper = base.upper().lstrip("$")
        for sym, info in markets.items():
            if info.base.upper() == base_upper:
                return sym
        return None

    # ── Diagnostics ──────────────────────────────────────────────────────

    async def get_connection_info(self) -> Dict[str, Any]:
        """
        Kumpulkan informasi koneksi untuk /status command.
        Return dict yang bisa ditampilkan di Telegram.
        """
        try:
            balance = await self.fetch_balance()
            markets = await self.fetch_all_markets()
            mode = "SANDBOX" if self._sandbox else "LIVE"
            return {
                "status": "connected",
                "mode": mode,
                "total_equity": f"{balance.total_equity:.2f} USDT",
                "free_margin": f"{balance.free_margin:.2f} USDT",
                "market_count": len(markets),
                "market_cache_age_minutes": round(
                    (time.time() - BitgetRestClient._market_cache_loaded_at) / 60, 1
                ),
            }
        except CriticalError as exc:
            return {"status": "error_critical", "error": str(exc)}
        except TransientError as exc:
            return {"status": "error_transient", "error": str(exc)}
        except Exception as exc:
            return {"status": "error_unknown", "error": str(exc)}


# ── Singleton ─────────────────────────────────────────────────────────────────
# Default client — dipakai oleh executor, risk engine, dll.
# Untuk testing: inject client palsu / mock, JANGAN andalkan singleton ini
# karena butuh koneksi network asli + API key valid.

_default_client: Optional[BitgetRestClient] = None


def get_rest_client() -> BitgetRestClient:
    """
    Return singleton BitgetRestClient.
    Dibuat lazy — exchange connection belum dibuka sampai pertama kali dipakai.
    """
    global _default_client
    if _default_client is None:
        _default_client = BitgetRestClient()
    return _default_client


async def reset_rest_client() -> None:
    """
    Tutup dan reset singleton client.
    Berguna saat circuit breaker HALF_OPEN test, atau saat shutdown.
    """
    global _default_client
    if _default_client is not None:
        await _default_client.close()
        _default_client = None
        logger.info("[rest_client] Singleton client reset")
