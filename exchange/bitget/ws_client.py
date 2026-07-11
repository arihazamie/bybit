"""
exchange/bitget/ws_client.py
=============================
Bitget Futures WebSocket client — monitoring realtime untuk order & posisi.

Tanggung jawab modul ini (Step 8):
  1. `watch_orders()` & `watch_positions()` via `ccxt.pro` — event seperti entry
     limit ke-fill atau SL kena terdeteksi dalam hitungan detik (push-based dari
     exchange), BUKAN lewat polling REST berkala yang delay-nya lebih lama.
  2. Reconnect otomatis dengan exponential backoff saat koneksi WebSocket putus
     (network blip, restart exchange, dsb) — loop TIDAK pernah berhenti permanen
     selama client masih `running`, terus mencoba reconnect.
  3. Fallback REST polling periodik (default tiap 15 detik, dikonfigurasi via
     `WS_RECONCILE_INTERVAL_SECONDS` di .env / `config.settings`) sebagai
     reconciliation — memastikan tidak ada event yang ter-miss kalau
     WebSocket sempat putus sebentar tanpa exception (silent gap) atau
     event ter-lewat saat reconnect.

PENTING — batas tanggung jawab Step 8:
  - Modul ini HANYA monitoring (read-only stream + reconciliation). Ia tidak
    mengambil keputusan bisnis (set SL, update status trade di DB, dsb) — itu
    tanggung jawab Executor (Step 12-13) yang akan di-wire lewat callback
    `on_order` / `on_position` di Step 19 (integrasi penuh).
  - Circuit breaker DB state (Step 14) belum disentuh di sini. Error WebSocket
    diklasifikasikan (transient/critical) dan di-log, tapi trip circuit breaker
    yang sesungguhnya (tulis ke tabel `circuit_breaker_state`) baru di-wire nanti.

Pemakaian dasar:
    async def handle_order(event: OrderEvent):
        print(event.symbol, event.status, event.filled)

    async def handle_position(event: PositionEvent):
        print(event.symbol, event.side, event.contracts)

    client = BitgetWsClient(on_order=handle_order, on_position=handle_position)
    await client.start()
    ...
    await client.stop()
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

import ccxt
import ccxt.pro as ccxtpro

from config.settings import settings
from core.logging_setup import get_logger
from exchange.bitget.rest_client import BitgetRestClient
from exchange.bitget.retry import classify_exception

logger = get_logger(__name__)


# ── Reconnect backoff (WebSocket) ───────────────────────────────────────────
# Beda dari RETRY_BACKOFF_SECONDS (REST, 3x percobaan lalu CriticalError) —
# WebSocket harus terus mencoba reconnect TANPA BATAS selama client `running`,
# jadi backoff naik bertahap lalu plateau di angka maksimum (tidak spam reconnect
# tiap detik, tapi juga tidak berhenti mencoba).
WS_RECONNECT_BACKOFF_SECONDS: tuple[float, ...] = (2, 5, 15, 30)
WS_RECONNECT_MAX_BACKOFF_SECONDS: float = 60.0


# ── Helpers ──────────────────────────────────────────────────────────────────

def _f(value: Any) -> Optional[float]:
    """Konversi value ke float dengan aman — return None jika kosong/invalid."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ── Data containers ──────────────────────────────────────────────────────────

@dataclass
class OrderEvent:
    """
    Event order yang sudah dinormalisasi dari raw ccxt order dict
    (hasil `watch_orders()` atau reconciliation `fetch_open_orders()`).

    `trigger_price` berguna untuk membedakan order biasa vs stop/trigger order
    (Stop Loss Bitget futures dieksekusi sebagai trigger order).
    """
    symbol: str
    order_id: str
    status: str                       # 'open' | 'closed' | 'canceled' | 'expired' | ...
    side: Optional[str]                # 'buy' | 'sell'
    order_type: Optional[str]          # 'limit' | 'market' | 'stop' | dll
    price: Optional[float]
    average: Optional[float]           # harga fill rata-rata
    filled: float
    remaining: float
    trigger_price: Optional[float]     # harga trigger untuk stop/SL order
    reduce_only: bool
    timestamp_ms: Optional[int]
    source: str                        # 'websocket' | 'reconciliation'
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)
    received_at: float = field(default_factory=time.time)

    @property
    def is_filled(self) -> bool:
        return self.status == "closed" and self.filled > 0

    @property
    def is_cancelled(self) -> bool:
        return self.status in ("canceled", "cancelled", "expired", "rejected")


@dataclass
class PositionEvent:
    """
    Event posisi yang sudah dinormalisasi dari raw ccxt position dict
    (hasil `watch_positions()` atau reconciliation `fetch_positions()`).
    """
    symbol: str
    side: Optional[str]                # 'long' | 'short'
    contracts: float                   # ukuran posisi saat ini (0 = posisi tertutup)
    entry_price: Optional[float]
    mark_price: Optional[float]
    liquidation_price: Optional[float]
    unrealized_pnl: Optional[float]
    leverage: Optional[float]
    margin_mode: Optional[str]         # 'cross' | 'isolated'
    timestamp_ms: Optional[int]
    source: str                        # 'websocket' | 'reconciliation'
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)
    received_at: float = field(default_factory=time.time)

    @property
    def is_closed(self) -> bool:
        return self.contracts == 0


OrderCallback = Callable[[OrderEvent], Awaitable[None]]
PositionCallback = Callable[[PositionEvent], Awaitable[None]]


# ── BitgetWsClient ───────────────────────────────────────────────────────────

class BitgetWsClient:
    """
    WebSocket client untuk Bitget Futures (USDT-M, mode Cross) — monitoring
    realtime via `ccxt.pro`, dengan reconnect otomatis + fallback REST
    reconciliation berkala.

    Tiga loop berjalan paralel selama client `start()`-ed:
      1. `_watch_orders_loop`    — stream `watch_orders()`, reconnect on error
      2. `_watch_positions_loop` — stream `watch_positions()`, reconnect on error
      3. `_reconciliation_loop`  — REST polling tiap `reconcile_interval` detik

    Semua event (dari WebSocket maupun reconciliation) dilewatkan ke callback
    `on_order` / `on_position` yang sama — konsumen (Executor, Step 12-13)
    cukup tahu field `source` untuk membedakan asalnya kalau perlu.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        passphrase: Optional[str] = None,
        sandbox: Optional[bool] = None,
        *,
        on_order: Optional[OrderCallback] = None,
        on_position: Optional[PositionCallback] = None,
        rest_client: Optional[BitgetRestClient] = None,
        reconcile_interval: Optional[float] = None,
    ) -> None:
        self._api_key = api_key or settings.BITGET_API_KEY
        self._api_secret = api_secret or settings.BITGET_API_SECRET
        self._passphrase = passphrase or settings.BITGET_PASSPHRASE
        self._sandbox = sandbox if sandbox is not None else settings.BITGET_USE_SANDBOX

        self._on_order = on_order
        self._on_position = on_position
        self._reconcile_interval = (
            reconcile_interval if reconcile_interval is not None
            else settings.WS_RECONCILE_INTERVAL_SECONDS
        )

        # REST client dipakai khusus untuk reconciliation fallback.
        self._rest = rest_client or BitgetRestClient(
            api_key=self._api_key,
            api_secret=self._api_secret,
            passphrase=self._passphrase,
            sandbox=self._sandbox,
        )

        self._ws_exchange: Optional[ccxtpro.bitget] = None
        self._ws_lock = asyncio.Lock()
        self._tasks: List[asyncio.Task] = []
        self._running = False

        # ── Diagnostics — dipakai /status command (Step 15) ────────────────
        self.ws_orders_connected: bool = False
        self.ws_positions_connected: bool = False
        self.last_order_event_at: Optional[float] = None
        self.last_position_event_at: Optional[float] = None
        self.last_reconcile_at: Optional[float] = None
        self.last_reconcile_error: Optional[str] = None
        self.orders_reconnect_count: int = 0
        self.positions_reconnect_count: int = 0

        # ── "Known open" cache — dipakai reconciliation untuk deteksi gap ──
        # Beberapa exchange (termasuk Bitget lewat ccxt.pro) TIDAK selalu
        # mengirim event eksplisit contracts=0 lewat watch_positions() saat
        # posisi ditutup (SL/TP/manual/liquidasi) — posisi itu cuma "hilang"
        # dari snapshot berikutnya. Kalau event WS itu ter-drop (mis. koneksi
        # putus tepat saat user close manual di web/app), reconciliation lama
        # TIDAK PERNAH bisa menangkapnya karena posisi yang sudah closed juga
        # tidak muncul lagi di fetch_positions() REST — bukan cuma di-skip.
        #
        # Fix: simpan simbol/pair yang terakhir diketahui MASIH open (dari
        # WS maupun reconciliation manapun). Setiap siklus reconciliation,
        # bandingkan simbol open sebelumnya vs simbol open saat ini — simbol
        # yang hilang dianggap closed/cancelled dan di-dispatch secara
        # sintetis, supaya cancel/close manual tetap tertangkap real-time
        # walau event WS aslinya ter-drop.
        self._known_open_position_symbols: set[str] = set()
        self._known_open_order_symbols: set[str] = set()

    # ── Lifecycle ────────────────────────────────────────────────────────

    async def _get_ws_exchange(self) -> ccxtpro.bitget:
        """Lazy-init ccxt.pro exchange object, thread/coroutine-safe via lock."""
        if self._ws_exchange is not None:
            return self._ws_exchange

        async with self._ws_lock:
            if self._ws_exchange is not None:
                return self._ws_exchange

            exchange = ccxtpro.bitget({
                "apiKey": self._api_key,
                "secret": self._api_secret,
                "password": self._passphrase,
                "enableRateLimit": True,
                "options": {
                    "defaultType": "swap",
                },
            })

            if self._sandbox:
                exchange.set_sandbox_mode(True)
                logger.info("[ws_client] Sandbox mode AKTIF — WebSocket ke Bitget Demo/Testnet")

            self._ws_exchange = exchange
            logger.info("[ws_client] ccxt.pro exchange diinisialisasi (sandbox=%s)", self._sandbox)

        return self._ws_exchange

    async def _reset_ws_exchange(self) -> None:
        """Tutup & buang exchange object yang sedang error, supaya loop berikutnya bikin baru."""
        async with self._ws_lock:
            if self._ws_exchange is not None:
                try:
                    await self._ws_exchange.close()
                except Exception as exc:
                    logger.debug("[ws_client] Error saat close ws exchange (diabaikan): %s", exc)
                finally:
                    self._ws_exchange = None

    async def start(self) -> None:
        """
        Jalankan ketiga loop (watch_orders, watch_positions, reconciliation)
        sebagai asyncio task paralel. Aman dipanggil dua kali (no-op jika sudah jalan).
        """
        if self._running:
            logger.warning("[ws_client] start() dipanggil tapi client sudah running — diabaikan")
            return

        self._running = True
        self._tasks = [
            asyncio.create_task(self._watch_orders_loop(), name="bitget_ws_watch_orders"),
            asyncio.create_task(self._watch_positions_loop(), name="bitget_ws_watch_positions"),
            asyncio.create_task(self._reconciliation_loop(), name="bitget_ws_reconciliation"),
        ]
        logger.info(
            "[ws_client] BitgetWsClient started — watch_orders + watch_positions aktif, "
            "reconciliation tiap %.0f detik",
            self._reconcile_interval,
        )

    async def stop(self) -> None:
        """Hentikan semua loop dengan rapi dan tutup koneksi WebSocket + REST."""
        if not self._running:
            return

        self._running = False
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []

        await self._reset_ws_exchange()
        await self._rest.close()
        logger.info("[ws_client] BitgetWsClient stopped")

    async def __aenter__(self) -> "BitgetWsClient":
        await self.start()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.stop()

    # ── Backoff helper ───────────────────────────────────────────────────

    @staticmethod
    def _backoff_for(attempt: int) -> float:
        """Backoff naik bertahap lalu plateau di WS_RECONNECT_MAX_BACKOFF_SECONDS."""
        if attempt < len(WS_RECONNECT_BACKOFF_SECONDS):
            return WS_RECONNECT_BACKOFF_SECONDS[attempt]
        return WS_RECONNECT_MAX_BACKOFF_SECONDS

    # ── watch_orders loop ────────────────────────────────────────────────

    async def _watch_orders_loop(self) -> None:
        """
        Loop tanpa henti: watch_orders() → dispatch event → ulangi.
        Kalau koneksi putus, reconnect dengan exponential backoff — loop ini
        TIDAK PERNAH berhenti permanen selama `self._running` True.
        """
        attempt = 0
        while self._running:
            try:
                exchange = await self._get_ws_exchange()
                raw_orders = await exchange.watch_orders()

                if not self.ws_orders_connected:
                    logger.info("[ws_client] watch_orders() tersambung")
                self.ws_orders_connected = True
                attempt = 0  # reset backoff setelah sukses

                for raw in raw_orders:
                    await self._dispatch_order(raw, source="websocket")

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.ws_orders_connected = False
                await self._handle_ws_error("watch_orders", exc, attempt)
                self.orders_reconnect_count += 1
                wait = self._backoff_for(attempt)
                attempt += 1
                await asyncio.sleep(wait)

    # ── watch_positions loop ─────────────────────────────────────────────

    async def _watch_positions_loop(self) -> None:
        """Sama seperti _watch_orders_loop, tapi untuk stream watch_positions()."""
        attempt = 0
        while self._running:
            try:
                exchange = await self._get_ws_exchange()
                raw_positions = await exchange.watch_positions()

                if not self.ws_positions_connected:
                    logger.info("[ws_client] watch_positions() tersambung")
                self.ws_positions_connected = True
                attempt = 0

                for raw in raw_positions:
                    await self._dispatch_position(raw, source="websocket")

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.ws_positions_connected = False
                await self._handle_ws_error("watch_positions", exc, attempt)
                self.positions_reconnect_count += 1
                wait = self._backoff_for(attempt)
                attempt += 1
                await asyncio.sleep(wait)

    async def _handle_ws_error(self, op: str, exc: Exception, attempt: int) -> None:
        """
        Klasifikasikan & log error WebSocket, lalu reset exchange object supaya
        percobaan berikutnya membuat koneksi baru (bukan reuse socket yang rusak).

        Catatan: tidak ada CriticalError yang di-raise di sini — untuk WebSocket,
        bahkan error "critical" seperti auth gagal tetap harus reconnect terus
        (operator perlu lihat alert & perbaiki API key), TAPI loop monitoring
        TIDAK boleh mati total (sesuai prinsip #10 — monitoring tidak pernah berhenti).
        """
        kind = classify_exception(exc) if isinstance(exc, ccxt.BaseError) else "transient"
        wait = self._backoff_for(attempt)
        logger.warning(
            "[ws_client] %s terputus (%s: %s) — reconnect dalam %.0f detik (percobaan ke-%d)",
            op, kind, exc, wait, attempt + 1,
        )
        await self._reset_ws_exchange()

    # ── Reconciliation loop (REST fallback) ──────────────────────────────

    async def _reconciliation_loop(self) -> None:
        """
        REST polling periodik (default tiap WS_RECONCILE_INTERVAL_SECONDS detik)
        sebagai jaring pengaman — bukan jalur utama deteksi realtime, tapi
        memastikan tidak ada event yang ter-miss kalau WebSocket sempat gap.
        """
        first_cycle = True
        while self._running:
            if not first_cycle:
                try:
                    await asyncio.sleep(self._reconcile_interval)
                except asyncio.CancelledError:
                    raise
            first_cycle = False

            if not self._running:
                break

            try:
                await self._reconcile_once()
                self.last_reconcile_at = time.time()
                self.last_reconcile_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_reconcile_error = str(exc)
                logger.error("[ws_client] Reconciliation gagal: %s", exc, exc_info=True)

    async def _reconcile_once(self) -> None:
        """
        Satu siklus reconciliation: fetch open orders + positions via REST,
        dispatch, LALU bandingkan dengan simbol yang sebelumnya diketahui
        open — simbol yang hilang (vanished) di-dispatch sebagai event
        sintetis contracts=0 / order closed-cancelled, supaya SL hit / TP
        hit / close manual / liquidated tetap tercover walau event WS asli
        untuk kejadian itu ter-drop (lihat docstring `_known_open_*_symbols`).
        """
        orders = await self._rest.fetch_open_orders()
        positions = await self._rest.fetch_positions()

        logger.debug(
            "[ws_client] Reconciliation: %d open order, %d posisi open (via REST)",
            len(orders), len(positions),
        )

        live_order_symbols = {raw.get("symbol") for raw in orders if raw.get("symbol")}
        live_position_symbols: set[str] = set()

        for raw in orders:
            await self._dispatch_order(raw, source="reconciliation")

        for raw in positions:
            contracts = _f(raw.get("contracts")) or 0.0
            symbol = raw.get("symbol")
            if contracts == 0:
                # Posisi kosong (sudah closed) — tidak perlu dispatch, hindari noise.
                continue
            if symbol:
                live_position_symbols.add(symbol)
            await self._dispatch_position(raw, source="reconciliation")

        # ── Deteksi gap: simbol yang SEBELUMNYA open tapi sekarang hilang ──
        vanished_positions = self._known_open_position_symbols - live_position_symbols
        for symbol in vanished_positions:
            await self._dispatch_synthetic_closed_position(symbol, source="reconciliation")

        vanished_orders = self._known_open_order_symbols - live_order_symbols
        for symbol in vanished_orders:
            # Kalau sekarang ada posisi live untuk simbol itu → order ke-fill.
            # Kalau tidak → order dibatalkan manual (atau expired) di exchange.
            filled = symbol in live_position_symbols or symbol in self._known_open_position_symbols
            await self._dispatch_synthetic_vanished_order(symbol, source="reconciliation", filled=filled)

    # ── Parsing & dispatch ───────────────────────────────────────────────

    def _parse_order(self, raw: Dict[str, Any], source: str) -> OrderEvent:
        """Normalisasi raw ccxt order dict (unified) ke OrderEvent."""
        info = raw.get("info") or {}
        return OrderEvent(
            symbol=raw.get("symbol", ""),
            order_id=str(raw.get("id") or info.get("orderId") or ""),
            status=raw.get("status") or "unknown",
            side=raw.get("side"),
            order_type=raw.get("type"),
            price=_f(raw.get("price")),
            average=_f(raw.get("average")),
            filled=_f(raw.get("filled")) or 0.0,
            remaining=_f(raw.get("remaining")) or 0.0,
            trigger_price=_f(raw.get("triggerPrice") or raw.get("stopPrice") or info.get("triggerPrice")),
            reduce_only=bool(raw.get("reduceOnly") or info.get("reduceOnly")),
            timestamp_ms=raw.get("timestamp"),
            source=source,
            raw=raw,
        )

    def _parse_position(self, raw: Dict[str, Any], source: str) -> PositionEvent:
        """Normalisasi raw ccxt position dict (unified) ke PositionEvent."""
        return PositionEvent(
            symbol=raw.get("symbol", ""),
            side=raw.get("side"),
            contracts=_f(raw.get("contracts")) or 0.0,
            entry_price=_f(raw.get("entryPrice")),
            mark_price=_f(raw.get("markPrice")),
            liquidation_price=_f(raw.get("liquidationPrice")),
            unrealized_pnl=_f(raw.get("unrealizedPnl")),
            leverage=_f(raw.get("leverage")),
            margin_mode=raw.get("marginMode"),
            timestamp_ms=raw.get("timestamp"),
            source=source,
            raw=raw,
        )

    async def _dispatch_order(self, raw: Dict[str, Any], source: str) -> None:
        event = self._parse_order(raw, source)
        self.last_order_event_at = time.time()

        # Update known-open cache — dipakai reconciliation untuk deteksi gap.
        if event.status == "open":
            self._known_open_order_symbols.add(event.symbol)
        else:
            self._known_open_order_symbols.discard(event.symbol)

        logger.info(
            "[ws_client][%s] Order %s %s — status=%s filled=%s/%s%s",
            source, event.symbol, event.order_id, event.status,
            event.filled, event.filled + event.remaining,
            " (trigger)" if event.trigger_price else "",
        )
        if self._on_order is None:
            return
        try:
            await self._on_order(event)
        except Exception as exc:
            logger.error("[ws_client] on_order callback error untuk %s: %s", event.symbol, exc, exc_info=True)

    async def _dispatch_position(self, raw: Dict[str, Any], source: str) -> None:
        event = self._parse_position(raw, source)
        self.last_position_event_at = time.time()

        # Update known-open cache — dipakai reconciliation untuk deteksi gap.
        if event.contracts > 0:
            self._known_open_position_symbols.add(event.symbol)
        else:
            self._known_open_position_symbols.discard(event.symbol)

        logger.info(
            "[ws_client][%s] Posisi %s %s — contracts=%s liq=%s",
            source, event.symbol, event.side, event.contracts, event.liquidation_price,
        )
        if self._on_position is None:
            return
        try:
            await self._on_position(event)
        except Exception as exc:
            logger.error("[ws_client] on_position callback error untuk %s: %s", event.symbol, exc, exc_info=True)

    async def _dispatch_synthetic_closed_position(self, symbol: str, source: str) -> None:
        """
        Dispatch event posisi closed (contracts=0) yang di-sintesis oleh
        reconciliation — dipakai saat sebuah simbol yang SEBELUMNYA diketahui
        open tiba-tiba hilang dari snapshot fetch_positions() REST, tanpa
        pernah ada event WS eksplisit contracts=0 untuk simbol itu (SL/TP
        hit, close manual, atau liquidated yang event WS-nya ter-drop).
        """
        event = PositionEvent(
            symbol=symbol,
            side=None,
            contracts=0.0,
            entry_price=None,
            mark_price=None,
            liquidation_price=None,
            unrealized_pnl=None,
            leverage=None,
            margin_mode=None,
            timestamp_ms=None,
            source=source,
            raw={},
        )
        self._known_open_position_symbols.discard(symbol)
        self.last_position_event_at = time.time()
        logger.warning(
            "[ws_client][%s] Posisi %s hilang dari snapshot exchange (bukan lewat event WS "
            "eksplisit) — kemungkinan besar SL/TP/close manual/liquidasi yang event-nya "
            "ter-drop. Dispatch sintetis contracts=0 supaya tetap tercover.",
            source, symbol,
        )
        if self._on_position is None:
            return
        try:
            await self._on_position(event)
        except Exception as exc:
            logger.error("[ws_client] on_position callback error (sintetis) untuk %s: %s", symbol, exc, exc_info=True)

    async def _dispatch_synthetic_vanished_order(self, symbol: str, source: str, filled: bool) -> None:
        """
        Dispatch event order closed/cancelled yang di-sintesis saat sebuah
        pending order untuk `symbol` hilang dari fetch_open_orders() REST
        tanpa pernah ada event WS eksplisit. `filled=True` jika ada posisi
        live untuk simbol itu sekarang (order ke-fill jadi posisi),
        `filled=False` jika tidak (order dibatalkan manual di web/app).
        """
        event = OrderEvent(
            symbol=symbol,
            order_id="",
            status="closed" if filled else "canceled",
            side=None,
            order_type=None,
            price=None,
            average=None,
            filled=0.0,
            remaining=0.0,
            trigger_price=None,
            reduce_only=False,
            timestamp_ms=None,
            source=source,
            raw={},
        )
        self._known_open_order_symbols.discard(symbol)
        self.last_order_event_at = time.time()
        logger.warning(
            "[ws_client][%s] Pending order %s hilang dari snapshot exchange (bukan lewat "
            "event WS eksplisit) — diklasifikasikan sebagai %s. Dispatch sintetis supaya "
            "cancel/fill manual tetap tercover.",
            source, symbol, event.status,
        )
        if self._on_order is None:
            return
        try:
            await self._on_order(event)
        except Exception as exc:
            logger.error("[ws_client] on_order callback error (sintetis) untuk %s: %s", symbol, exc, exc_info=True)

    # ── Seeding known-open state (startup) ──────────────────────────────
    # Dipanggil main.py SEBELUM start() dengan pair yang berstatus open/pending
    # di database. Tanpa ini, posisi/order yang closed/cancelled SAAT bot mati
    # (downtime) tidak akan pernah terdeteksi sebagai "vanished" oleh
    # reconciliation, karena awalnya cache kosong. Dengan seeding ini, siklus
    # reconciliation PERTAMA setelah start() langsung bisa mendeteksi gap itu.

    def seed_known_open_state(
        self,
        open_position_symbols: Optional[set[str]] = None,
        open_order_symbols: Optional[set[str]] = None,
    ) -> None:
        if open_position_symbols:
            self._known_open_position_symbols |= set(open_position_symbols)
        if open_order_symbols:
            self._known_open_order_symbols |= set(open_order_symbols)
        logger.info(
            "[ws_client] Known-open state di-seed dari database — positions=%s orders=%s",
            self._known_open_position_symbols, self._known_open_order_symbols,
        )

    # ── Diagnostics ──────────────────────────────────────────────────────

    def get_status(self) -> Dict[str, Any]:
        """Ringkasan status WebSocket untuk /status command (Step 15)."""
        now = time.time()
        return {
            "running": self._running,
            "ws_orders_connected": self.ws_orders_connected,
            "ws_positions_connected": self.ws_positions_connected,
            "last_order_event_seconds_ago": (
                round(now - self.last_order_event_at, 1) if self.last_order_event_at else None
            ),
            "last_position_event_seconds_ago": (
                round(now - self.last_position_event_at, 1) if self.last_position_event_at else None
            ),
            "last_reconcile_seconds_ago": (
                round(now - self.last_reconcile_at, 1) if self.last_reconcile_at else None
            ),
            "last_reconcile_error": self.last_reconcile_error,
            "orders_reconnect_count": self.orders_reconnect_count,
            "positions_reconnect_count": self.positions_reconnect_count,
        }


# ── Singleton ─────────────────────────────────────────────────────────────────
# Sama seperti get_rest_client() di rest_client.py — dipakai oleh main.py /
# komponen lain. Callback (on_order/on_position) hanya berlaku saat instance
# pertama kali dibuat; untuk mengganti callback, reset dulu via reset_ws_client().

_default_ws_client: Optional[BitgetWsClient] = None


def get_ws_client(
    on_order: Optional[OrderCallback] = None,
    on_position: Optional[PositionCallback] = None,
) -> BitgetWsClient:
    """Return singleton BitgetWsClient. Dibuat lazy — belum konek sampai start() dipanggil."""
    global _default_ws_client
    if _default_ws_client is None:
        _default_ws_client = BitgetWsClient(on_order=on_order, on_position=on_position)
    return _default_ws_client


async def reset_ws_client() -> None:
    """Stop dan reset singleton client. Berguna untuk testing & shutdown."""
    global _default_ws_client
    if _default_ws_client is not None:
        await _default_ws_client.stop()
        _default_ws_client = None
        logger.info("[ws_client] Singleton WS client reset")