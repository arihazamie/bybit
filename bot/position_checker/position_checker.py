"""
bot/position_checker/position_checker.py
==========================================
Step 11 — Position checker module (bagian 5 prompt.md).

Tanggung jawab modul ini:
  1. WAJIB cek posisi & order existing untuk satu pair SEBELUM eksekusi
     sinyal apapun. Bot harus "sadar" salah satu dari kondisi berikut:
       - Tidak ada posisi open & tidak ada pending order → eksekusi normal
       - Sudah ada posisi open untuk pair yang sama        → tawarkan opsi
       - Sudah ada pending limit order untuk pair yang sama → tawarkan opsi
  2. Selalu gabungkan data LIVE dari Bitget (fetch_positions /
     fetch_open_orders) + cross-check database lokal — live exchange adalah
     source of truth untuk "apakah posisi/order benar-benar ada", database
     lokal memberi konteks tambahan (entry asal sinyal, SL, TP, source
     analyst, trade_id) untuk ditampilkan ke user.
  3. Implementasi `position_conflict_mode` (skip / ask / add / replace) —
     resolve_conflict_action() murni menerjemahkan kondisi + mode aktif
     menjadi satu PositionAction, TANPA melakukan eksekusi apapun.

TIDAK dikerjakan di modul ini (sesuai pembagian tanggung jawab step):
  - Eksekusi order baru / cancel / close — itu tugas Bitget executor
    (Step 12-13). Modul ini hanya MEREKOMENDASIKAN aksi.
  - Inline button & timeout konfirmasi — itu tugas Step 18. Modul ini hanya
    menyiapkan teks & opsi aksi yang nanti dirender jadi tombol.

Alur tipikal (dipanggil dari pipeline Step 19, sebelum risk_engine/executor):

    check = await check_position_condition(pair, rest_client=client)
    if check.recommended_action == PositionAction.PROCEED:
        ... lanjut risk_engine → leverage_engine → executor ...
    elif check.recommended_action == PositionAction.SKIP:
        ... log + notifikasi singkat, sinyal diabaikan ...
    elif check.recommended_action == PositionAction.ASK_CONFIRMATION:
        ... kirim check.notification_text() + inline button (Step 18) ...
    elif check.recommended_action == PositionAction.ADD:
        ... lanjut eksekusi normal (posisi tambahan), kirim notifikasi info ...
    elif check.recommended_action == PositionAction.REPLACE:
        ... executor cancel pending lama / close posisi lama dulu (Step 12-13),
            baru lanjut eksekusi sinyal baru ...

Konvensi (konsisten dengan bot/risk_engine, bot/leverage_engine):
  - Fungsi murni (resolve_conflict_action, _classify_condition,
    format_*) — tidak ada I/O, mudah di-unit-test sendiri.
  - Orchestrator async (check_position_condition,
    fetch_live_position_for_pair, fetch_live_pending_order_for_pair) —
    melakukan I/O ke exchange + DB, menangkap error sebagai
    PositionCheckResult(success=False, ...) — tidak ada exception bocor
    ke caller, konsisten dengan RiskCalculationResult & LeverageSafetyResult.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from core.constants import Direction, PositionAction, PositionCondition
from core.logging_setup import get_logger
from db.crud.settings import async_get_position_conflict_mode
from db.crud.trades import async_get_open_trade_for_pair
from exchange.bitget.retry import CriticalError, TransientError
from exchange.bitget.rest_client import BitgetRestClient, get_rest_client

logger = get_logger(__name__)


# ── Helpers ──────────────────────────────────────────────────────────────

def _safe_float(value: Any, default: float = 0.0) -> float:
    """Konversi value ke float dengan aman — return default jika None/invalid."""
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# ── Data containers ──────────────────────────────────────────────────────

@dataclass
class LivePositionInfo:
    """
    Posisi open live dari Bitget (hasil fetch_positions), dinormalisasi.
    """
    symbol: str
    direction: str             # 'long' | 'short'
    contracts: float           # ukuran posisi (unit aset)
    entry_price: float
    unrealized_pnl: float
    leverage: Optional[float] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_long(self) -> bool:
        return self.direction == Direction.LONG

    def summary_line(self) -> str:
        sign = "+" if self.unrealized_pnl >= 0 else ""
        return (
            f"{self.symbol} {self.direction.upper()} | "
            f"entry={self.entry_price:g} | size={self.contracts:g} | "
            f"uPnL={sign}{self.unrealized_pnl:.4f} USDT"
        )


@dataclass
class LivePendingOrderInfo:
    """
    Pending limit order live dari Bitget (hasil fetch_open_orders),
    dinormalisasi. Order yang muncul di fetch_open_orders dianggap belum
    fill (status open) — itulah artinya "pending" di konteks modul ini.
    """
    symbol: str
    order_id: str
    direction: str              # 'long' (buy) | 'short' (sell)
    price: float
    amount: float
    order_type: str             # biasanya 'limit'
    raw: Dict[str, Any] = field(default_factory=dict)

    def summary_line(self) -> str:
        return (
            f"{self.symbol} {self.direction.upper()} {self.order_type} "
            f"@ {self.price:g} | qty={self.amount:g} | id={self.order_id}"
        )


@dataclass
class PositionCheckResult:
    """
    Hasil lengkap pengecekan kondisi pair sebelum eksekusi sinyal baru.
    Dikembalikan oleh check_position_condition() ke pipeline (Step 19).
    """
    success: bool
    pair: str

    condition: str = PositionCondition.NONE
    conflict_mode: str = "ask"
    recommended_action: str = PositionAction.PROCEED

    # Data live dari exchange (source of truth)
    live_position: Optional[LivePositionInfo] = None
    live_pending_order: Optional[LivePendingOrderInfo] = None

    # Data lokal (konteks tambahan — entry asal sinyal, SL, TP, dll.)
    db_trade: Optional[dict] = None

    # True jika live exchange menunjukkan posisi/order tapi DB lokal TIDAK
    # punya record yang cocok — kemungkinan posisi dibuka manual di luar bot.
    untracked_in_db: bool = False

    failure_reason: Optional[str] = None
    notes: list = field(default_factory=list)

    def notification_text(self) -> str:
        return format_position_check_notification(self)

    @property
    def has_conflict(self) -> bool:
        return self.condition != PositionCondition.NONE


@dataclass
class ConflictActionOption:
    """
    Satu opsi aksi yang bisa diambil user saat konflik posisi terdeteksi.
    Dipakai Step 18 untuk merender inline keyboard — modul ini hanya
    menyiapkan daftar opsi & label, bukan tombol Telegram itu sendiri.
    """
    action: str           # PositionAction.*
    label: str            # teks tombol, mis. "➕ Tambah Posisi"
    description: str      # penjelasan singkat


# ── Fungsi murni — klasifikasi kondisi & resolusi aksi ───────────────────

def _classify_condition(
    has_position: bool,
    has_pending_order: bool,
) -> str:
    """
    Klasifikasikan kondisi pair berdasarkan flag live exchange.

    Murni — tidak ada I/O, gampang di-unit-test.
    """
    if has_position and has_pending_order:
        return PositionCondition.OPEN_AND_PENDING
    if has_position:
        return PositionCondition.OPEN_POSITION
    if has_pending_order:
        return PositionCondition.PENDING_ORDER
    return PositionCondition.NONE


def resolve_conflict_action(condition: str, conflict_mode: str) -> str:
    """
    Terjemahkan kondisi pair + position_conflict_mode aktif menjadi satu
    PositionAction yang direkomendasikan — bagian 5 prompt.md.

    Murni — tidak ada I/O, tidak ada efek samping.

    Aturan:
      - condition == NONE              → selalu PROCEED, apapun mode-nya
      - condition != NONE & mode=skip  → SKIP (abaikan sinyal baru otomatis)
      - condition != NONE & mode=ask   → ASK_CONFIRMATION (default, JANGAN
                                          auto-overwrite tanpa konfirmasi)
      - condition != NONE & mode=add   → ADD (buka posisi tambahan)
      - condition != NONE & mode=replace → REPLACE (cancel/close lama dulu)
      - mode tidak dikenali             → fallback paling aman: ASK_CONFIRMATION
    """
    if condition == PositionCondition.NONE:
        return PositionAction.PROCEED

    mode = (conflict_mode or "ask").strip().lower()

    if mode == "skip":
        return PositionAction.SKIP
    if mode == "add":
        return PositionAction.ADD
    if mode == "replace":
        return PositionAction.REPLACE
    if mode == "ask":
        return PositionAction.ASK_CONFIRMATION

    # Mode tidak dikenali (data settings korup/tidak valid) → fallback aman
    logger.warning(
        "[position_checker] position_conflict_mode '%s' tidak dikenali — "
        "fallback ke 'ask' (paling aman)",
        conflict_mode,
    )
    return PositionAction.ASK_CONFIRMATION


def get_conflict_action_options(condition: str) -> List[ConflictActionOption]:
    """
    Daftar opsi aksi yang relevan untuk satu kondisi konflik — dipakai
    Step 18 untuk merender inline keyboard saat mode='ask'.

    Untuk OPEN_POSITION:  Tambah / Abaikan / Replace (close lama + entry baru)
    Untuk PENDING_ORDER:  Update harga / Biarkan order lama / Cancel + entry baru
    Untuk OPEN_AND_PENDING: gabungan dari keduanya, tawarkan opsi paling umum
    """
    if condition == PositionCondition.PENDING_ORDER:
        return [
            ConflictActionOption(
                action=PositionAction.REPLACE,
                label="🔄 Update Entry (Replace)",
                description="Cancel pending order lama, pasang entry baru dari sinyal",
            ),
            ConflictActionOption(
                action=PositionAction.SKIP,
                label="🚫 Biarkan Order Lama",
                description="Abaikan sinyal baru, pending order lama tetap jalan",
            ),
            ConflictActionOption(
                action=PositionAction.ADD,
                label="➕ Tambah Order Baru",
                description="Pasang entry baru tanpa cancel order lama",
            ),
        ]

    # OPEN_POSITION dan OPEN_AND_PENDING pakai set opsi yang sama
    return [
        ConflictActionOption(
            action=PositionAction.ADD,
            label="➕ Tambah Posisi",
            description="Buka posisi tambahan untuk pair ini (double exposure)",
        ),
        ConflictActionOption(
            action=PositionAction.SKIP,
            label="🚫 Abaikan Sinyal",
            description="Posisi lama tetap, sinyal baru tidak dieksekusi",
        ),
        ConflictActionOption(
            action=PositionAction.REPLACE,
            label="🔁 Replace Posisi Lama",
            description="Close posisi lama, buka posisi baru sesuai sinyal",
        ),
    ]


# ── Parsing data live exchange ────────────────────────────────────────────

def _parse_live_position(raw_position: Dict[str, Any]) -> Optional[LivePositionInfo]:
    """
    Parse satu raw ccxt position dict menjadi LivePositionInfo.
    Return None jika posisi sebenarnya kosong (contracts == 0 — sudah closed).
    """
    contracts = _safe_float(
        raw_position.get("contracts") or raw_position.get("contractSize")
    )
    if contracts == 0:
        return None

    side = (raw_position.get("side") or "").lower()
    direction = Direction.SHORT if side == "short" else Direction.LONG

    return LivePositionInfo(
        symbol=raw_position.get("symbol", ""),
        direction=direction,
        contracts=abs(contracts),
        entry_price=_safe_float(raw_position.get("entryPrice")),
        unrealized_pnl=_safe_float(raw_position.get("unrealizedPnl")),
        leverage=_safe_float(raw_position.get("leverage"), default=None) or None,
        raw=raw_position,
    )


def _parse_live_pending_order(raw_order: Dict[str, Any]) -> Optional[LivePendingOrderInfo]:
    """
    Parse satu raw ccxt order dict menjadi LivePendingOrderInfo.
    fetch_open_orders() hanya mengembalikan order yang BELUM fill, jadi
    setiap entry yang masuk sini dianggap pending.
    """
    side = (raw_order.get("side") or "").lower()
    direction = Direction.SHORT if side == "sell" else Direction.LONG

    return LivePendingOrderInfo(
        symbol=raw_order.get("symbol", ""),
        order_id=str(raw_order.get("id") or raw_order.get("orderId") or ""),
        direction=direction,
        price=_safe_float(raw_order.get("price")),
        amount=_safe_float(raw_order.get("amount")),
        order_type=(raw_order.get("type") or "limit"),
        raw=raw_order,
    )


# ── Orchestrator async — I/O ke exchange + DB ─────────────────────────────

async def fetch_live_position_for_pair(
    rest_client: BitgetRestClient,
    pair: str,
) -> Optional[LivePositionInfo]:
    """
    Fetch posisi open live untuk satu pair via REST.
    Return None jika tidak ada posisi open (contracts == 0 atau list kosong).

    Raises: CriticalError / TransientError (dari rest_client, sudah
    diklasifikasikan oleh @with_retry — caller (check_position_condition)
    yang menangkap menjadi PositionCheckResult(success=False, ...)).
    """
    raw_positions = await rest_client.fetch_positions([pair])
    for raw in raw_positions:
        if raw.get("symbol") != pair:
            continue
        parsed = _parse_live_position(raw)
        if parsed is not None:
            return parsed
    return None


async def fetch_live_pending_order_for_pair(
    rest_client: BitgetRestClient,
    pair: str,
) -> Optional[LivePendingOrderInfo]:
    """
    Fetch pending limit order live (belum fill) untuk satu pair via REST.
    Jika ada lebih dari satu pending order untuk pair yang sama, return yang
    PALING BARU (asumsi: list dari ccxt biasanya urut waktu — kita ambil
    elemen terakhir sebagai fallback paling aman; jika ccxt menyertakan
    'timestamp' kita urutkan eksplisit).

    Return None jika tidak ada pending order.
    """
    raw_orders = await rest_client.fetch_open_orders(pair)
    if not raw_orders:
        return None

    # Urutkan berdasarkan timestamp (terbaru dulu) jika tersedia, supaya
    # hasil deterministik walau ccxt tidak menjamin urutan.
    def _ts(o: Dict[str, Any]) -> float:
        return _safe_float(o.get("timestamp"))

    raw_orders_sorted = sorted(raw_orders, key=_ts, reverse=True)
    return _parse_live_pending_order(raw_orders_sorted[0])


async def check_position_condition(
    pair: str,
    rest_client: Optional[BitgetRestClient] = None,
) -> PositionCheckResult:
    """
    Cek kondisi pair SEBELUM eksekusi sinyal baru — fungsi utama Step 11.

    WAJIB dipanggil oleh pipeline (Step 19) sebelum risk_engine/executor
    memproses sinyal apapun untuk pair tertentu, sesuai bagian 5 prompt.md.

    Alur:
      1. Fetch posisi live dari Bitget untuk pair ini (REST fetch_positions)
      2. Fetch pending order live dari Bitget untuk pair ini (REST
         fetch_open_orders)
      3. Cross-check dengan database lokal (get_open_trade_for_pair) — untuk
         konteks tambahan (entry/SL/TP asal sinyal, source_analyst, trade_id)
      4. Klasifikasikan kondisi (none / open_position / pending_order /
         open_and_pending) — live exchange adalah source of truth
      5. Ambil position_conflict_mode aktif dari settings
      6. Resolve recommended_action sesuai kondisi + mode

    Tidak pernah raise exception ke caller — error exchange ditangkap
    menjadi PositionCheckResult(success=False, failure_reason=...).
    """
    client = rest_client or get_rest_client()
    notes: list = []

    try:
        live_position = await fetch_live_position_for_pair(client, pair)
        live_pending_order = await fetch_live_pending_order_for_pair(client, pair)
    except CriticalError as exc:
        logger.error("[position_checker] CriticalError saat cek %s: %s", pair, exc)
        return PositionCheckResult(
            success=False,
            pair=pair,
            failure_reason=f"exchange_error: {exc}",
        )
    except TransientError as exc:
        logger.error(
            "[position_checker] TransientError (retry habis) saat cek %s: %s",
            pair, exc,
        )
        return PositionCheckResult(
            success=False,
            pair=pair,
            failure_reason=f"transient_error_exhausted: {exc}",
        )

    # Cross-check database lokal — konteks tambahan, BUKAN source of truth
    # untuk "apakah posisi ada" (live exchange selalu menang).
    db_trade: Optional[dict] = None
    try:
        db_trade = await async_get_open_trade_for_pair(pair)
    except Exception as exc:  # pragma: no cover — DB lokal tidak boleh crash check
        logger.warning(
            "[position_checker] Gagal query database lokal untuk %s: %s — "
            "lanjut pakai data live exchange saja",
            pair, exc,
        )
        notes.append(f"db_lookup_failed: {exc}")

    has_position = live_position is not None
    has_pending = live_pending_order is not None
    condition = _classify_condition(has_position, has_pending)

    untracked_in_db = bool((has_position or has_pending) and db_trade is None)
    if untracked_in_db:
        notes.append(
            "Posisi/order terdeteksi di exchange tapi TIDAK ada record yang "
            "cocok di database lokal — kemungkinan dibuka manual di luar bot."
        )

    try:
        conflict_mode = await async_get_position_conflict_mode()
    except Exception as exc:  # pragma: no cover — settings tidak boleh crash check
        logger.warning(
            "[position_checker] Gagal baca position_conflict_mode: %s — "
            "fallback ke 'ask' (paling aman)",
            exc,
        )
        conflict_mode = "ask"
        notes.append(f"conflict_mode_lookup_failed_fallback_ask: {exc}")

    recommended_action = resolve_conflict_action(condition, conflict_mode)

    result = PositionCheckResult(
        success=True,
        pair=pair,
        condition=condition,
        conflict_mode=conflict_mode,
        recommended_action=recommended_action,
        live_position=live_position,
        live_pending_order=live_pending_order,
        db_trade=db_trade,
        untracked_in_db=untracked_in_db,
        notes=notes,
    )

    logger.info(
        "[position_checker] %s → condition=%s, conflict_mode=%s, action=%s",
        pair, condition, conflict_mode, recommended_action,
    )
    return result


# ── Notifikasi ─────────────────────────────────────────────────────────────

def format_position_check_notification(result: PositionCheckResult) -> str:
    """
    Format teks notifikasi Telegram untuk hasil position check — dipakai
    sebagai isi pesan konfirmasi konflik (Step 18 menambahkan inline button
    di atas teks ini, tidak mengubah isinya).
    """
    if not result.success:
        return (
            f"❌ Gagal cek kondisi posisi untuk {result.pair}\n"
            f"Alasan: {result.failure_reason}\n"
            f"⚠️ Sinyal TIDAK dieksekusi sampai status posisi bisa dipastikan."
        )

    if result.condition == PositionCondition.NONE:
        return f"✅ {result.pair}: tidak ada posisi/pending order — aman untuk eksekusi normal."

    lines = [f"🔁 Konflik posisi terdeteksi untuk {result.pair}"]

    if result.live_position is not None:
        lines.append(f"📌 Posisi open: {result.live_position.summary_line()}")
    if result.live_pending_order is not None:
        lines.append(f"⏳ Pending order: {result.live_pending_order.summary_line()}")

    if result.db_trade is not None:
        sl = result.db_trade.get("sl_price")
        tp = result.db_trade.get("tp_price")
        analyst = result.db_trade.get("source_analyst") or "-"
        lines.append(
            f"🗂️ Database: SL={sl}, TP={tp or 'belum diset'}, analyst={analyst}"
        )
    if result.untracked_in_db:
        lines.append("⚠️ Tidak ada record cocok di database lokal (kemungkinan manual).")

    mode_label = {
        "skip": "SKIP (abaikan otomatis)",
        "ask": "ASK (tunggu konfirmasi manual)",
        "add": "ADD (tambah posisi otomatis)",
        "replace": "REPLACE (ganti posisi lama otomatis)",
    }.get(result.conflict_mode, result.conflict_mode)
    lines.append(f"⚙️ Mode konflik aktif: {mode_label}")

    if result.recommended_action == PositionAction.ASK_CONFIRMATION:
        lines.append("👉 Mau Tambah posisi / Abaikan sinyal / Replace posisi lama?")
    elif result.recommended_action == PositionAction.SKIP:
        lines.append("🚫 Sinyal baru diabaikan otomatis sesuai mode 'skip'.")
    elif result.recommended_action == PositionAction.ADD:
        lines.append("➕ Posisi tambahan akan dibuka otomatis sesuai mode 'add'.")
    elif result.recommended_action == PositionAction.REPLACE:
        lines.append("🔁 Posisi/order lama akan diganti otomatis sesuai mode 'replace'.")

    return "\n".join(lines)
