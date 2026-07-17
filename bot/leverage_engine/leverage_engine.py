"""
bot/leverage_engine/leverage_engine.py
========================================
Step 10 — Leverage safety engine, cross-aware.

Tanggung jawab modul ini:
  1. Proyeksi liquidation gabungan seluruh akun (snapshot equity +
     maintenance margin semua posisi open + posisi baru simulasi)
     — sesuai bagian 4.3 prompt.md.
  2. Validasi SL posisi baru vs proyeksi liquidation gabungan tsb,
     dengan buffer 5-10%.
  3. Auto-adjust leverage turun satu langkah kalau max leverage tidak aman —
     cari leverage minimum yang membuat posisi aman (atau setidak-tidaknya
     memberikan buffer terbesar yang bisa dicapai). TIDAK PERNAH membatalkan
     trade — tapi kirim notifikasi informatif kalau leverage diturunkan.
  4. Re-check semua posisi existing setiap kali ada posisi baru dibuka atau
     ditutup — karena mode cross berbagi pool margin, membuka satu posisi bisa
     menggeser jarak-ke-liquidation posisi LAIN.

TIDAK dikerjakan di modul ini (sesuai pembagian tanggung jawab step):
  - Eksekusi order (Step 12-13)
  - Set leverage ke exchange (rest_client.set_leverage — dipanggil Step 12)
  - Circuit breaker (Step 14)

Alur tipikal (dipanggil dari pipeline Step 19, sesudah risk_engine Step 9):

    risk_result = await calculate_trade_risk(...)   # Step 9 → risk_amount, position_size, leverage default
    safety_result = await run_leverage_safety_check(
        pair=pair,
        direction=direction,
        entry_price=risk_result.entry_price_used,
        sl_price=risk_result.sl_price,
        position_size=risk_result.position_size,
        initial_leverage=risk_result.leverage_used,    # default dari Step 9
        max_leverage_available=risk_result.max_leverage_available,
        rest_client=client,
    )
    if safety_result.leverage_adjusted:
        risk_result.recompute_margin(safety_result.leverage_safe)
        # kirim notifikasi leverage turun

Konvensi:
  - Fungsi murni (_project_*, _check_*, _estimate_*) — tidak ada I/O,
    mudah di-unit-test sendiri.
  - Orchestrator async (run_leverage_safety_check, recheck_existing_positions)
    — melakukan I/O ke exchange dan DB, menangkap error sebagai
    LeverageSafetyResult(success=False, ...) — tidak ada exception yang bocor
    ke caller.

Matematika liquidation gabungan (mode cross):
    Dalam mode cross, akun dilikuidasi ketika:
        total_equity + sum(unrealized_pnl semua posisi) < sum(maintenance_margin semua posisi)

    Karena unrealized_pnl setiap posisi bergantung pada harga pasar,
    kita menghitung "liquidation price" sebagai harga posisi BARU yang
    menyebabkan kondisi di atas terpenuhi — dengan asumsi posisi lain
    tetap (harga lain tidak bergerak):

        equity_buffer = total_equity - total_existing_mm - new_mm

        Untuk LONG:
            liq_price_new = entry_price - equity_buffer / position_size
        Untuk SHORT:
            liq_price_new = entry_price + equity_buffer / position_size

    Buffer aman (default 7%, bisa dikonfigurasi):
        LONG  → aman jika liq_price_new <= sl_price * (1 - buffer_pct)
                — artinya liquidation setidaknya buffer% di bawah SL
        SHORT → aman jika liq_price_new >= sl_price * (1 + buffer_pct)
                — artinya liquidation setidaknya buffer% di atas SL

    Ini adalah estimasi konservatif — asumsi posisi lain tidak bergerak adalah
    worst-case tidak terpenuhi di dunia nyata, tapi lebih safe daripada
    asumsikan semua hedged sempurna.

Maintenance margin rate:
    Diperoleh dari raw market info Bitget (field 'maintainMarginRate' atau
    'minMaintainMarginRate' di info kontrak). Fallback: 0.5% dari notional
    (konservatif untuk leverage tinggi). Rate nyata bervariasi per tier
    margin — kita pakai tier pertama (tier notional kecil) sebagai default
    karena tujuannya adalah estimasi, bukan perhitungan exact.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from config.settings import settings
from core.constants import Direction, EventType, Severity
from core.logging_setup import get_logger
from db.crud.event_log import async_log_event
from exchange.bitget.rest_client import BitgetRestClient, get_rest_client
from exchange.bitget.retry import CriticalError, TransientError

logger = get_logger(__name__)

# ── Konstanta default ──────────────────────────────────────────────────────
DEFAULT_SAFETY_BUFFER_PCT = 0.07          # 7% buffer default antara liq dan SL
FALLBACK_MAINTENANCE_MARGIN_RATE = 0.005  # 0.5% fallback jika tidak bisa fetch
MIN_LEVERAGE = 1                           # leverage minimum yang bisa dicoba
MAX_LEVERAGE_SEARCH_STEPS = 50            # maksimal iterasi binary search


# ── Data containers ─────────────────────────────────────────────────────────

@dataclass
class OpenPositionSnapshot:
    """
    Snapshot ringkas posisi open — dipakai untuk kalkulasi maintenance margin
    gabungan. Data ini dikumpulkan dari fetch_positions() REST atau WebSocket.
    """
    symbol: str
    direction: str                # 'long' | 'short'
    contracts: float              # jumlah kontrak (units)
    entry_price: float            # average entry price
    notional: float               # contracts * entry_price (approximate)
    maintenance_margin: float     # maintenance margin yang dikunci exchange
    maintenance_margin_rate: float  # rate yang dipakai (0.005 = 0.5%)
    sl_price: Optional[float] = None   # SL yang diketahui (dari database / exchange)
    unrealized_pnl: float = 0.0

    @property
    def is_long(self) -> bool:
        return self.direction == Direction.LONG


@dataclass
class LiquidationProjection:
    """
    Hasil proyeksi liquidation gabungan untuk satu posisi baru.

    Liquidation "price" adalah harga posisi baru yang akan menyebabkan
    seluruh akun dilikuidasi (asumsi posisi lain statis).
    """
    entry_price: float
    position_size: float
    leverage_used: float
    maintenance_margin_rate: float

    # Equity & margin snapshot saat kalkulasi
    total_equity: float
    total_existing_mm: float       # total maintenance margin posisi lain
    new_position_mm: float         # maintenance margin posisi baru ini

    # Hasil proyeksi
    equity_buffer: float           # = total_equity - total_existing_mm - new_position_mm
    liquidation_price: float       # harga dimana seluruh akun dilikuidasi

    # Safety check
    sl_price: float
    direction: str
    buffer_pct: float
    is_safe: bool                  # apakah SL cukup jauh dari liquidation?
    sl_to_liq_distance_pct: float  # jarak SL ke liq sebagai % dari SL harga


@dataclass
class LeverageSafetyResult:
    """
    Hasil lengkap safety check + auto-adjustment leverage.
    Dikembalikan oleh run_leverage_safety_check() ke pipeline (Step 19).
    """
    success: bool

    # Leverage sebelum & sesudah adjustment
    leverage_requested: float       # leverage dari risk_engine (Step 9)
    leverage_safe: float            # leverage yang aman (mungkin sama, mungkin lebih rendah)
    leverage_adjusted: bool         # True jika leverage diturunkan dari yang diminta

    # Proyeksi pada leverage_safe
    projection: Optional[LiquidationProjection] = None

    # Apakah BAHKAN leverage terendah pun tidak aman (peringatan kritis)
    even_min_leverage_unsafe: bool = False

    # Info posisi yang ada saat snapshot
    existing_positions_count: int = 0
    total_existing_mm: float = 0.0
    total_equity_snapshot: float = 0.0

    # Kegagalan
    failure_reason: Optional[str] = None
    notes: list = field(default_factory=list)

    def notification_text(self) -> str:
        return format_leverage_safety_notification(self)


@dataclass
class ExistingPositionSafetyAlert:
    """
    Alert untuk posisi existing yang jadi tidak aman setelah posisi baru dibuka/ditutup.
    """
    symbol: str
    direction: str
    sl_price: Optional[float]
    liq_price_estimate: float
    sl_to_liq_distance_pct: float
    is_safe: bool
    entry_price: float
    position_size: float


# ── Fungsi murni (tidak ada I/O) ─────────────────────────────────────────────

def _estimate_maintenance_margin_rate(market_raw: Optional[Dict[str, Any]]) -> float:
    """
    Estimasi maintenance margin rate dari raw market info Bitget.

    Prioritas pencarian field:
      1. market_raw['info']['maintainMarginRate']   — paling eksplisit
      2. market_raw['info']['minMaintainMarginRate']
      3. market_raw['info']['mmRate']
      4. market_raw['maintenanceMarginRate']         — ccxt unified (kadang ada)
      5. FALLBACK_MAINTENANCE_MARGIN_RATE (0.5%)     — jika semua kosong

    Catatan: Rate yang sesungguhnya bervariasi per tier notional, tapi kita
    pakai rate minimum (tier terkecil = notional kecil) karena tujuannya
    adalah estimasi konservatif, bukan kalkulasi exact liquidation.
    """
    if not market_raw:
        return FALLBACK_MAINTENANCE_MARGIN_RATE

    # Coba jalur ccxt unified dulu
    ccxt_mm = market_raw.get("maintenanceMarginRate")
    if ccxt_mm is not None:
        try:
            rate = float(ccxt_mm)
            if 0 < rate < 1:
                return rate
        except (TypeError, ValueError):
            pass

    # Coba raw Bitget info
    info = market_raw.get("info") or {}
    for field_name in ("maintainMarginRate", "minMaintainMarginRate", "mmRate"):
        val = info.get(field_name)
        if val is not None:
            try:
                rate = float(val)
                if 0 < rate < 1:
                    return rate
            except (TypeError, ValueError):
                pass

    logger.debug(
        "[leverage_engine] Tidak bisa ambil maintenance margin rate dari market info — "
        "pakai fallback %.1f%%", FALLBACK_MAINTENANCE_MARGIN_RATE * 100
    )
    return FALLBACK_MAINTENANCE_MARGIN_RATE


def _calculate_new_position_mm(
    position_size: float,
    entry_price: float,
    leverage_used: float,
    maintenance_margin_rate: float,
) -> float:
    """
    Hitung maintenance margin untuk posisi baru.

    Formula: mm = (position_size * entry_price) * maintenance_margin_rate
    (Maintenance margin dihitung dari notional value, bukan margin yang dikunci)

    Args:
        position_size          : ukuran posisi (unit aset)
        entry_price            : harga entry
        leverage_used          : leverage yang akan dipakai
        maintenance_margin_rate: rate maintenance margin (mis. 0.005 = 0.5%)
    """
    notional = position_size * entry_price
    mm = notional * maintenance_margin_rate
    return mm


def _project_liquidation_price(
    *,
    direction: str,
    entry_price: float,
    position_size: float,
    leverage_used: float,
    maintenance_margin_rate: float,
    total_equity: float,
    total_existing_mm: float,
    sl_price: float,
    buffer_pct: float = DEFAULT_SAFETY_BUFFER_PCT,
) -> LiquidationProjection:
    """
    Proyeksikan harga liquidation gabungan jika posisi baru dibuka.

    Lihat docstring modul untuk penjelasan matematika.

    Raises:
        ValueError: parameter tidak valid (position_size <= 0, dll.)
    """
    if position_size <= 0:
        raise ValueError(f"position_size harus > 0, dapat: {position_size}")
    if entry_price <= 0:
        raise ValueError(f"entry_price harus > 0, dapat: {entry_price}")
    if leverage_used <= 0:
        raise ValueError(f"leverage_used harus > 0, dapat: {leverage_used}")
    if total_equity < 0:
        raise ValueError(f"total_equity tidak boleh negatif, dapat: {total_equity}")
    if total_existing_mm < 0:
        raise ValueError(f"total_existing_mm tidak boleh negatif, dapat: {total_existing_mm}")

    # Maintenance margin posisi baru
    new_mm = _calculate_new_position_mm(
        position_size, entry_price, leverage_used, maintenance_margin_rate
    )

    # Equity buffer = sisa equity setelah semua maintenance margin dipenuhi
    equity_buffer = total_equity - total_existing_mm - new_mm

    # Proyeksi harga liquidation
    if direction == Direction.LONG:
        # LONG: liquidation saat harga turun dari entry
        # loss = position_size * (entry_price - liq_price) = equity_buffer
        # liq_price = entry_price - equity_buffer / position_size
        liq_price = entry_price - equity_buffer / position_size
    else:
        # SHORT: liquidation saat harga naik dari entry
        # loss = position_size * (liq_price - entry_price) = equity_buffer
        # liq_price = entry_price + equity_buffer / position_size
        liq_price = entry_price + equity_buffer / position_size

    # Safety check: apakah SL cukup jauh dari liquidation?
    if direction == Direction.LONG:
        # LONG: liq harus LEBIH RENDAH dari SL (dengan buffer)
        # Aman jika: sl_price - liq_price >= buffer_pct * sl_price
        # yaitu: liq_price <= sl_price * (1 - buffer_pct)
        safe_liq_threshold = sl_price * (1 - buffer_pct)
        is_safe = liq_price <= safe_liq_threshold
        # Jarak SL ke liq sebagai % dari SL (positif = liq di bawah SL)
        sl_to_liq_distance_pct = (sl_price - liq_price) / sl_price if sl_price > 0 else 0.0
    else:
        # SHORT: liq harus LEBIH TINGGI dari SL (dengan buffer)
        safe_liq_threshold = sl_price * (1 + buffer_pct)
        is_safe = liq_price >= safe_liq_threshold
        sl_to_liq_distance_pct = (liq_price - sl_price) / sl_price if sl_price > 0 else 0.0

    return LiquidationProjection(
        entry_price=entry_price,
        position_size=position_size,
        leverage_used=leverage_used,
        maintenance_margin_rate=maintenance_margin_rate,
        total_equity=total_equity,
        total_existing_mm=total_existing_mm,
        new_position_mm=new_mm,
        equity_buffer=equity_buffer,
        liquidation_price=liq_price,
        sl_price=sl_price,
        direction=direction,
        buffer_pct=buffer_pct,
        is_safe=is_safe,
        sl_to_liq_distance_pct=sl_to_liq_distance_pct,
    )


def _find_safe_leverage(
    *,
    direction: str,
    entry_price: float,
    position_size: float,
    max_leverage: float,
    maintenance_margin_rate: float,
    total_equity: float,
    total_existing_mm: float,
    sl_price: float,
    buffer_pct: float = DEFAULT_SAFETY_BUFFER_PCT,
) -> Tuple[float, LiquidationProjection, bool]:
    """
    Cari leverage tertinggi yang masih aman (binary search integer).

    Return:
        (leverage_safe, projection_at_safe_leverage, even_min_unsafe)
        - leverage_safe: leverage tertinggi yang aman, atau MIN_LEVERAGE jika
          bahkan leverage terendah pun tidak aman
        - projection_at_safe_leverage: proyeksi di leverage_safe
        - even_min_unsafe: True jika bahkan leverage=1 pun tidak aman
    """
    # Cek apakah max leverage sudah aman
    proj_max = _project_liquidation_price(
        direction=direction,
        entry_price=entry_price,
        position_size=position_size,
        leverage_used=max_leverage,
        maintenance_margin_rate=maintenance_margin_rate,
        total_equity=total_equity,
        total_existing_mm=total_existing_mm,
        sl_price=sl_price,
        buffer_pct=buffer_pct,
    )
    if proj_max.is_safe:
        return max_leverage, proj_max, False

    # Binary search: cari integer leverage tertinggi yang aman
    # (mulai dari max_leverage dan turun ke MIN_LEVERAGE)
    lo, hi = MIN_LEVERAGE, int(max_leverage)
    best_lev = lo
    best_proj = _project_liquidation_price(
        direction=direction,
        entry_price=entry_price,
        position_size=position_size,
        leverage_used=float(lo),
        maintenance_margin_rate=maintenance_margin_rate,
        total_equity=total_equity,
        total_existing_mm=total_existing_mm,
        sl_price=sl_price,
        buffer_pct=buffer_pct,
    )

    if not best_proj.is_safe:
        # Bahkan leverage=1 pun tidak aman
        return float(MIN_LEVERAGE), best_proj, True

    # Leverage 1 aman — cari yang tertinggi yang masih aman
    steps = 0
    while lo <= hi and steps < MAX_LEVERAGE_SEARCH_STEPS:
        mid = (lo + hi) // 2
        if mid <= 0:
            break

        proj_mid = _project_liquidation_price(
            direction=direction,
            entry_price=entry_price,
            position_size=position_size,
            leverage_used=float(mid),
            maintenance_margin_rate=maintenance_margin_rate,
            total_equity=total_equity,
            total_existing_mm=total_existing_mm,
            sl_price=sl_price,
            buffer_pct=buffer_pct,
        )

        if proj_mid.is_safe:
            best_lev = mid
            best_proj = proj_mid
            lo = mid + 1
        else:
            hi = mid - 1

        steps += 1

    return float(best_lev), best_proj, False


def _check_existing_position_safety(
    pos: OpenPositionSnapshot,
    *,
    total_equity: float,
    total_other_mm: float,   # total MM semua posisi LAIN (tidak termasuk pos ini)
    buffer_pct: float = DEFAULT_SAFETY_BUFFER_PCT,
) -> ExistingPositionSafetyAlert:
    """
    Cek apakah posisi existing masih aman setelah perubahan equity/MM akun.

    Catatan: Untuk posisi yang sudah open, kita tidak bisa mengubah leverage-nya
    (leverage fixed saat posisi dibuka). Yang kita cek adalah apakah proyeksi
    liquidation dari sisi posisi ini tetap aman relatif terhadap SL yang diketahui.
    """
    # Re-project liquidation dari sudut pandang posisi existing ini
    equity_buffer = total_equity - total_other_mm - pos.maintenance_margin
    position_size = pos.contracts

    if position_size <= 0:
        position_size = 1e-10  # guard division by zero

    if pos.is_long:
        liq_price = pos.entry_price - equity_buffer / position_size
    else:
        liq_price = pos.entry_price + equity_buffer / position_size

    # Safety check terhadap SL posisi ini (jika diketahui)
    if pos.sl_price is not None and pos.sl_price > 0:
        if pos.is_long:
            safe_threshold = pos.sl_price * (1 - buffer_pct)
            is_safe = liq_price <= safe_threshold
            sl_to_liq_pct = (pos.sl_price - liq_price) / pos.sl_price
        else:
            safe_threshold = pos.sl_price * (1 + buffer_pct)
            is_safe = liq_price >= safe_threshold
            sl_to_liq_pct = (liq_price - pos.sl_price) / pos.sl_price
    else:
        # SL tidak diketahui — tandai sebagai tidak aman jika equity_buffer sangat tipis
        # (< 5% dari total equity)
        is_safe = equity_buffer > (total_equity * 0.05)
        sl_to_liq_pct = equity_buffer / max(total_equity, 1e-10)

    return ExistingPositionSafetyAlert(
        symbol=pos.symbol,
        direction=pos.direction,
        sl_price=pos.sl_price,
        liq_price_estimate=liq_price,
        sl_to_liq_distance_pct=sl_to_liq_pct,
        is_safe=is_safe,
        entry_price=pos.entry_price,
        position_size=position_size,
    )


# ── Parse posisi dari ccxt raw response ──────────────────────────────────────

def _parse_open_positions(
    raw_positions: List[Dict[str, Any]],
    market_info_map: Dict[str, Any],
) -> List[OpenPositionSnapshot]:
    """
    Parse list raw ccxt position dict ke OpenPositionSnapshot.

    Filter: hanya posisi dengan contracts > 0 (posisi aktif).
    maintenance_margin_rate diambil dari market_info_map[symbol].raw
    jika tersedia, fallback ke FALLBACK_MAINTENANCE_MARGIN_RATE.
    """
    result: List[OpenPositionSnapshot] = []

    for pos in raw_positions:
        symbol = pos.get("symbol", "")
        contracts = float(pos.get("contracts") or 0)
        if contracts <= 0:
            continue   # posisi sudah closed atau kosong

        side = str(pos.get("side") or "").lower()
        direction = Direction.LONG if side == "long" else Direction.SHORT

        entry_price = float(pos.get("entryPrice") or pos.get("averagePrice") or 0)
        if entry_price <= 0:
            logger.debug("[leverage_engine] Skip posisi %s — entry price 0", symbol)
            continue

        notional = float(pos.get("notional") or 0)
        if notional <= 0:
            notional = contracts * entry_price

        # Ambil maintenance margin dari exchange jika tersedia
        mm_from_pos = float(pos.get("maintenanceMargin") or 0)
        mm_rate_from_pos = float(pos.get("maintenanceMarginPercentage") or 0)

        # Ambil market info untuk rate
        market_raw = market_info_map.get(symbol)
        mm_rate = _estimate_maintenance_margin_rate(market_raw)

        if mm_rate_from_pos > 0:
            mm_rate = mm_rate_from_pos

        # Gunakan MM langsung dari exchange jika tersedia (lebih akurat)
        if mm_from_pos > 0:
            mm = mm_from_pos
        else:
            mm = notional * mm_rate

        unrealized_pnl = float(pos.get("unrealizedPnl") or pos.get("unrealizedProfit") or 0)

        # SL dari exchange (jika ada — biasanya tidak exposed langsung di ccxt)
        sl_price = None
        raw_info = pos.get("info") or {}
        for sl_field in ("stopLossPrice", "stopLoss", "slPrice"):
            val = raw_info.get(sl_field)
            if val and float(val) > 0:
                sl_price = float(val)
                break

        result.append(OpenPositionSnapshot(
            symbol=symbol,
            direction=direction,
            contracts=contracts,
            entry_price=entry_price,
            notional=notional,
            maintenance_margin=mm,
            maintenance_margin_rate=mm_rate,
            sl_price=sl_price,
            unrealized_pnl=unrealized_pnl,
        ))

    return result


# ── Orchestrator async ────────────────────────────────────────────────────────

async def run_leverage_safety_check(
    *,
    pair: str,
    direction: str,
    entry_price: float,
    sl_price: float,
    position_size: float,
    initial_leverage: float,
    max_leverage_available: float,
    rest_client: Optional[BitgetRestClient] = None,
    safety_buffer_pct: float = DEFAULT_SAFETY_BUFFER_PCT,
) -> LeverageSafetyResult:
    """
    Jalankan safety check liquidation vs SL untuk posisi baru — bagian 4.3 prompt.md.

    Entry point utama modul ini, dipanggil oleh pipeline (Step 19) sesudah
    risk_engine (Step 9) menghasilkan initial_leverage, position_size, entry_price.

    Args:
        pair               : unified symbol Bitget (mis. "STG/USDT:USDT")
        direction          : Direction.LONG atau Direction.SHORT
        entry_price        : harga entry (dari sinyal atau estimasi ticker)
        sl_price           : harga stop loss dari sinyal
        position_size      : ukuran posisi dalam unit aset (dari risk_engine)
        initial_leverage   : leverage default dari risk_engine (max atau cap user)
        max_leverage_available: max leverage asli dari exchange (sebelum cap user)
        rest_client        : override client (untuk unit test). Default: singleton.
        safety_buffer_pct  : buffer antara liq dan SL (default 7%)

    Returns:
        LeverageSafetyResult — success=False untuk semua kegagalan exchange.
        Lihat field leverage_safe untuk leverage yang harus dipakai saat entry.
    """
    client = rest_client or get_rest_client()
    result = LeverageSafetyResult(
        success=False,
        leverage_requested=initial_leverage,
        leverage_safe=initial_leverage,  # default sama, diupdate jika diturunkan
        leverage_adjusted=False,
    )

    # ── 1. Snapshot akun: equity + posisi open existing ──────────────────
    try:
        balance = await client.fetch_balance()
        result.total_equity_snapshot = balance.total_equity
    except (CriticalError, TransientError) as exc:
        result.failure_reason = "exchange_error_balance"
        result.notes.append(f"Gagal fetch balance untuk safety check: {exc}")
        logger.error("[leverage_engine] %s", result.notes[-1])
        return result

    try:
        raw_positions = await client.fetch_positions()
    except (CriticalError, TransientError) as exc:
        result.failure_reason = "exchange_error_positions"
        result.notes.append(f"Gagal fetch posisi untuk safety check: {exc}")
        logger.error("[leverage_engine] %s", result.notes[-1])
        return result

    # ── 2. Parse & hitung total maintenance margin posisi existing ───────
    try:
        markets = await client.fetch_all_markets()
        market_raw_map = {sym: info.raw for sym, info in markets.items()}
    except (CriticalError, TransientError):
        market_raw_map = {}   # fallback: pakai rate default saja

    existing_positions = _parse_open_positions(raw_positions, market_raw_map)
    total_existing_mm = sum(p.maintenance_margin for p in existing_positions)

    result.existing_positions_count = len(existing_positions)
    result.total_existing_mm = total_existing_mm

    # ── 3. Ambil maintenance margin rate untuk pair baru ini ─────────────
    new_market_raw = market_raw_map.get(pair)
    mm_rate = _estimate_maintenance_margin_rate(new_market_raw)

    # ── 4. Safety check pada leverage awal ──────────────────────────────
    total_equity = balance.total_equity

    try:
        proj_initial = _project_liquidation_price(
            direction=direction,
            entry_price=entry_price,
            position_size=position_size,
            leverage_used=initial_leverage,
            maintenance_margin_rate=mm_rate,
            total_equity=total_equity,
            total_existing_mm=total_existing_mm,
            sl_price=sl_price,
            buffer_pct=safety_buffer_pct,
        )
    except ValueError as exc:
        result.failure_reason = "invalid_parameters"
        result.notes.append(f"Parameter tidak valid untuk proyeksi liquidation: {exc}")
        logger.error("[leverage_engine] %s", result.notes[-1])
        return result

    if proj_initial.is_safe:
        # Leverage awal sudah aman — tidak perlu adjustment
        result.success = True
        result.leverage_safe = initial_leverage
        result.leverage_adjusted = False
        result.projection = proj_initial
        logger.info(
            "[leverage_engine] %s | leverage=%dx AMAN | liq=%.6f | sl=%.6f | buffer=%.1f%%",
            pair, int(initial_leverage),
            proj_initial.liquidation_price, sl_price,
            proj_initial.sl_to_liq_distance_pct * 100,
        )
        return result

    # ── 5. Leverage awal tidak aman ───────────────────────────────────
    # PENTING (akun mode CROSS): maintenance margin posisi baru = notional *
    # mm_rate, TIDAK bergantung leverage_used (lihat _calculate_new_position_mm)
    # — di cross, seluruh equity akun jadi collateral bersama; leverage per
    # simbol hanya mengubah margin_needed/free margin, BUKAN jarak akun ke
    # liquidation untuk position_size yang tetap. Konsekuensinya: kalau
    # proyeksi di initial_leverage sudah tidak aman, proyeksi TETAP tidak
    # aman di leverage berapa pun (termasuk 1x) — tidak ada "leverage aman"
    # untuk dicari (dulu di sini ada binary search _find_safe_leverage yang
    # secara matematis selalu berakhir di initial_leverage atau MIN_LEVERAGE
    # tanpa pernah menemukan titik tengah — dead code, sudah dihapus).
    # Satu-satunya cara sesungguhnya memperbesar buffer liquidation-vs-SL
    # adalah memperkecil position_size (risk_amount lebih kecil / sl_distance
    # lebih lebar) — bukan leverage.
    logger.info(
        "[leverage_engine] %s | leverage=%dx TIDAK AMAN — liq=%.6f terlalu dekat sl=%.6f "
        "(buffer=%.1f%%, butuh %.1f%%). Mode cross: leverage tidak mengubah proyeksi "
        "liquidation untuk position_size tetap — tidak ada leverage lain yang akan aman.",
        pair, int(initial_leverage),
        proj_initial.liquidation_price, sl_price,
        proj_initial.sl_to_liq_distance_pct * 100,
        safety_buffer_pct * 100,
    )

    leverage_safe = initial_leverage
    proj_safe = proj_initial
    even_min_unsafe = True

    result.success = True
    result.leverage_safe = leverage_safe
    result.leverage_adjusted = False
    result.projection = proj_safe
    result.even_min_leverage_unsafe = even_min_unsafe

    # ── FORCE_MAX_LEVERAGE override ───────────────────────────────────
    # Leverage SELALU dipakai di initial_leverage (max leverage_available
    # atau cap manual /setleverage) — ini sekarang juga perilaku default
    # untuk kasus "tidak aman", karena menurunkan leverage tidak memberi
    # keamanan tambahan di mode cross (lihat komentar di atas). max_loss
    # tetap dijamin 1% oleh risk_engine (position_size dari risk_amount/
    # sl_distance, independen dari leverage) — trade-off yang disadari:
    # kalau harga gap/slip melewati SL tanpa sempat fill, liquidation bisa
    # lebih dulu terjadi dengan kerugian > 1%.
    if settings.FORCE_MAX_LEVERAGE:
        result.leverage_safe = initial_leverage
        result.leverage_adjusted = False
        result.projection = proj_initial

    if even_min_unsafe and settings.FORCE_MAX_LEVERAGE:
        msg = (
            f"⚠️ Proyeksi liquidation untuk {pair} sangat dekat dengan SL bahkan "
            f"di leverage minimum (1x) — total exposure akun kemungkinan sudah "
            f"besar. FORCE_MAX_LEVERAGE aktif: leverage TETAP dipakai di "
            f"{int(initial_leverage)}x sesuai konfigurasi (bukan diturunkan). "
            f"max_loss tetap 1% by design SELAMA SL sempat fill — risiko gap/"
            f"slip melewati SL tetap ada, PERIKSA exposure manual kalau perlu."
        )
        result.notes.append(msg)
        logger.warning("[leverage_engine] %s", msg)

        try:
            await async_log_event(
                event_type=EventType.LIQUIDATION_WARNING,
                message=msg,
                severity=Severity.CRITICAL,
                component="leverage_engine",
            )
        except Exception as exc:
            logger.warning("[leverage_engine] Gagal log event: %s", exc)

    elif even_min_unsafe:
        msg = (
            f"⚠️ Bahkan leverage terendah (1x) tidak memberi buffer aman antara "
            f"proyeksi liquidation dan SL untuk {pair}. "
            f"Total exposure akun kemungkinan sudah sangat besar. "
            f"Tetap dijalankan di leverage minimum — PERIKSA exposure manual."
        )
        result.notes.append(msg)
        logger.warning("[leverage_engine] %s", msg)

        # Log ke event_log sebagai CRITICAL warning
        try:
            await async_log_event(
                event_type=EventType.LIQUIDATION_WARNING,
                message=msg,
                severity=Severity.CRITICAL,
                component="leverage_engine",
            )
        except Exception as exc:
            logger.warning("[leverage_engine] Gagal log event: %s", exc)

    # Catatan: cabang "leverage_adjusted" (leverage diturunkan ke nilai
    # tengah demi keamanan) sengaja DIHAPUS — di mode cross, menurunkan
    # leverage tidak pernah benar-benar mengurangi risiko liquidation untuk
    # position_size yang tetap (lihat komentar di langkah 5 di atas), jadi
    # hasil selalu jatuh ke salah satu dari dua cabang even_min_unsafe di
    # atas: aman di initial_leverage (return awal proj_initial.is_safe), atau
    # tidak aman di leverage manapun.

    logger.info(
        "[leverage_engine] %s | leverage_safe=%dx | liq=%.6f | sl=%.6f | "
        "buffer=%.1f%% | adjusted=%s | even_min_unsafe=%s | force_max_leverage=%s",
        pair, int(result.leverage_safe),
        result.projection.liquidation_price, sl_price,
        result.projection.sl_to_liq_distance_pct * 100,
        result.leverage_adjusted,
        result.even_min_leverage_unsafe,
        settings.FORCE_MAX_LEVERAGE,
    )

    return result


async def recheck_existing_positions(
    *,
    rest_client: Optional[BitgetRestClient] = None,
    safety_buffer_pct: float = DEFAULT_SAFETY_BUFFER_PCT,
    sl_lookup: Optional[Dict[str, float]] = None,
) -> List[ExistingPositionSafetyAlert]:
    """
    Re-check semua posisi existing setelah ada posisi baru dibuka atau ditutup.

    Sesuai bagian 4.3 langkah 5:
    'WAJIB DIULANG setiap kali ADA POSISI BARU DIBUKA ATAU DITUTUP: re-check
    seluruh posisi open lain yang masih berjalan.'

    Return:
        List ExistingPositionSafetyAlert — hanya yang is_safe=False.
        Caller (pipeline Step 19 / executor Step 12-13) bertanggung jawab
        mengirim alert Telegram untuk setiap item di list ini.

    Args:
        sl_lookup : dict {symbol: sl_price} dari database lokal — melengkapi
                    SL yang mungkin tidak ter-expose di ccxt position response.
    """
    client = rest_client or get_rest_client()
    sl_lookup = sl_lookup or {}

    try:
        balance = await client.fetch_balance()
        total_equity = balance.total_equity
    except (CriticalError, TransientError) as exc:
        logger.error("[leverage_engine] recheck — gagal fetch balance: %s", exc)
        return []

    try:
        raw_positions = await client.fetch_positions()
    except (CriticalError, TransientError) as exc:
        logger.error("[leverage_engine] recheck — gagal fetch positions: %s", exc)
        return []

    try:
        markets = await client.fetch_all_markets()
        market_raw_map = {sym: info.raw for sym, info in markets.items()}
    except (CriticalError, TransientError):
        market_raw_map = {}

    existing_positions = _parse_open_positions(raw_positions, market_raw_map)

    # Inject SL dari database lokal jika posisi tidak punya SL dari exchange
    for pos in existing_positions:
        if pos.sl_price is None and pos.symbol in sl_lookup:
            pos.sl_price = sl_lookup[pos.symbol]

    total_all_mm = sum(p.maintenance_margin for p in existing_positions)
    unsafe_alerts: List[ExistingPositionSafetyAlert] = []

    for pos in existing_positions:
        # MM untuk posisi ini
        # MM posisi lain = total - MM posisi ini
        mm_others = total_all_mm - pos.maintenance_margin

        alert = _check_existing_position_safety(
            pos,
            total_equity=total_equity,
            total_other_mm=mm_others,
            buffer_pct=safety_buffer_pct,
        )

        if not alert.is_safe:
            unsafe_alerts.append(alert)
            logger.warning(
                "[leverage_engine] Posisi %s %s TIDAK AMAN — "
                "liq=%.6f, sl=%.6f, buffer=%.1f%%",
                pos.symbol, pos.direction,
                alert.liq_price_estimate,
                pos.sl_price or 0,
                alert.sl_to_liq_distance_pct * 100,
            )

    if not unsafe_alerts:
        logger.debug(
            "[leverage_engine] recheck — %d posisi existing semua aman",
            len(existing_positions),
        )

    return unsafe_alerts


# ── Format notifikasi ─────────────────────────────────────────────────────────

def format_leverage_safety_notification(result: LeverageSafetyResult) -> str:
    """
    Format notifikasi untuk Telegram — dipakai pipeline (Step 19) dan
    Telegram bot (Step 15-17).
    """
    if not result.success:
        reason_map = {
            "exchange_error_balance": "Gagal fetch balance akun.",
            "exchange_error_positions": "Gagal fetch posisi existing.",
            "invalid_parameters": "Parameter posisi tidak valid.",
        }
        reason_text = reason_map.get(result.failure_reason or "", "Alasan tidak diketahui.")
        lines = [
            f"❌ Leverage safety check GAGAL — {reason_text}",
        ]
        if result.notes:
            lines.append(f"   Detail: {result.notes[-1]}")
        return "\n".join(lines)

    proj = result.projection
    lines = []

    if result.even_min_leverage_unsafe:
        lines.append("🚨 PERINGATAN KRITIS — Leverage safety check:")
        if settings.FORCE_MAX_LEVERAGE:
            lines.append(
                f"   Bahkan leverage minimum (1x) TIDAK memberi jarak aman dari "
                f"liquidation — tapi FORCE_MAX_LEVERAGE aktif, leverage TETAP "
                f"dipakai di {int(result.leverage_safe)}x (bukan diturunkan)."
            )
            lines.append(
                "   max_loss tetap 1% SELAMA SL sempat fill — risiko gap/slip "
                "melewati SL tetap ada."
            )
        else:
            lines.append(
                "   Bahkan leverage 1x TIDAK memberi jarak aman dari liquidation."
            )
            lines.append("   Total exposure akun terlalu besar — pertimbangkan kurangi posisi!")
    elif result.leverage_adjusted:
        lines.append("⚠️ Leverage otomatis diturunkan (safety check cross mode):")
        lines.append(
            f"   {int(result.leverage_requested)}x → {int(result.leverage_safe)}x"
        )
        lines.append(
            "   (risk_amount & position_size TIDAK berubah, hanya margin_needed berubah)"
        )
    else:
        lines.append("✅ Leverage safety check: OK")

    if proj:
        lines.append(
            f"   • Proyeksi liquidation gabungan: {proj.liquidation_price:.6f}"
        )
        lines.append(
            f"   • Stop loss: {proj.sl_price:.6f}  "
            f"(jarak ke liq: {proj.sl_to_liq_distance_pct*100:.1f}%, buffer min: {proj.buffer_pct*100:.0f}%)"
        )
        lines.append(
            f"   • Leverage dipakai: {int(result.leverage_safe)}x"
        )

    if result.existing_positions_count > 0:
        lines.append(
            f"   • Posisi open existing: {result.existing_positions_count} "
            f"(total MM: {result.total_existing_mm:.2f} USDT)"
        )

    return "\n".join(lines)


def format_existing_position_alert(alert: ExistingPositionSafetyAlert) -> str:
    """Format alert untuk satu posisi existing yang tidak aman."""
    sl_text = f"{alert.sl_price:.6f}" if alert.sl_price else "tidak diketahui"
    dir_text = "LONG 📈" if alert.direction == Direction.LONG else "SHORT 📉"
    return (
        f"⚠️ Posisi {alert.symbol} {dir_text} terancam liquidation:\n"
        f"   • Entry: {alert.entry_price:.6f}\n"
        f"   • SL: {sl_text}\n"
        f"   • Proyeksi liquidation: {alert.liq_price_estimate:.6f}\n"
        f"   • Jarak SL→Liq: {alert.sl_to_liq_distance_pct*100:.1f}%\n"
        f"   Akibat posisi baru yang baru dibuka/ditutup — pertimbangkan kurangi exposure."
    )