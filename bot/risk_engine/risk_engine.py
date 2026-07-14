"""
bot/risk_engine/risk_engine.py
===============================
Step 9 — Risk & margin engine. Bagian PALING KRUSIAL dari seluruh sistem
(lihat bagian 4 prompt.md) — kesalahan kecil di sini berakibat langsung
ke modal.

Tugas modul ini:
  1. Hitung `risk_amount` sesuai mode aktif (Percent / Fixed USD) — bagian 4.1
  2. Hitung `position_size` dari risk_amount & jarak SL — bagian 4.4
     (position_size TIDAK PERNAH dipengaruhi leverage)
  3. Hitung `margin_needed` dari position_size, entry_price, leverage_used
     — leverage HANYA mengubah margin_needed, bukan position_size/risk
  4. Validasi `margin_needed <= free_margin` (saldo fisik cukup)
  5. Hasilkan notifikasi yang TEGAS membedakan dua angka yang sering tertukar:
       - risk_amount (= kerugian aktual JIKA SL hit — selalu konstan)
       - margin_needed (= margin yang dikunci exchange — berbeda tiap trade)

BUKAN tugas modul ini (Step 10 — leverage_engine):
  - Safety check liquidation price vs SL di mode Cross (bagian 4.3)
  - Auto-adjust leverage turun demi keamanan
  - Re-check posisi lain saat ada posisi baru dibuka/ditutup

Step 9 memakai `leverage_used = max_leverage_available` (atau cap manual user
via /setleverage, lihat bagian 4.2) sebagai DEFAULT SEMENTARA. Step 10 akan
memanggil ulang `calculate_margin_needed()` dengan `leverage_used` yang sudah
disesuaikan demi keamanan liquidation — risk_amount & position_size TIDAK
PERNAH berubah karena leverage, hanya margin_needed yang berubah.

Konvensi modul ini (konsisten dengan bot/parser/*):
  - Fungsi murni (calculate_*) — tidak ada I/O, mudah di-unit-test sendiri,
    raise ValueError untuk input yang secara matematis tidak valid.
  - Orchestrator async (calculate_trade_risk) — yang melakukan I/O (fetch
    balance, max leverage, ticker) dan MENANGKAP error menjadi
    RiskCalculationResult(success=False, ...) — bukan exception — supaya
    pipeline (Step 19) bisa memutuskan alur lanjutan tanpa try/except
    bertingkat, konsisten dengan SignalEvaluation di bot/parser/ambiguity.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from config.settings import settings
from core.constants import Direction, EntryType, RiskMode
from core.logging_setup import get_logger
from db.crud.settings import async_get_leverage_cap, async_get_risk_amount_config
from exchange.bitget.retry import CriticalError, TransientError
from exchange.bitget.rest_client import BitgetRestClient, get_rest_client

logger = get_logger(__name__)


# ── Hasil kalkulasi ───────────────────────────────────────────────────────

@dataclass
class RiskCalculationResult:
    """
    Hasil lengkap kalkulasi risk & margin untuk satu kandidat trade.

    PENTING — dua angka berikut JANGAN PERNAH ditukar saat ditampilkan ke
    user (lihat bagian 4.1 & 4.4 prompt.md):
      - risk_amount_usd : kerugian aktual JIKA SL hit. Ini KONSTAN — tidak
                           peduli leverage berapa atau coin apa.
      - margin_needed   : margin yang DIKUNCI exchange untuk posisi ini.
                           Ini BERBEDA-BEDA tiap trade (tergantung jarak SL
                           & leverage) — bukan bug, memang begitu cara
                           kerjanya.
    """

    success: bool

    # ── Risk policy (bagian 4.1) ────────────────────────────────────────
    risk_mode: str = RiskMode.PERCENT            # 'percent' | 'fixed_usd'
    risk_amount_usd: float = 0.0                 # = kerugian aktual jika SL hit
    risk_percent_used: Optional[float] = None    # terisi hanya jika mode=percent

    # ── Harga & jarak SL ─────────────────────────────────────────────────
    entry_price_used: Optional[float] = None     # harga yang dipakai utk kalkulasi
    entry_price_estimated: bool = False          # True jika diambil dari ticker
                                                  # (market order tanpa harga eksplisit)
    sl_price: Optional[float] = None
    sl_distance: Optional[float] = None

    # ── Position size & margin (bagian 4.4) ─────────────────────────────
    position_size: Optional[float] = None        # unit aset — TIDAK dipengaruhi leverage
    max_leverage_available: Optional[float] = None
    leverage_used: Optional[float] = None         # DEFAULT (belum lewat safety check Step 10)
    leverage_capped_by_user: bool = False         # True jika dibatasi /setleverage
    margin_needed: Optional[float] = None

    # ── Validasi saldo ───────────────────────────────────────────────────
    total_equity: Optional[float] = None          # dipakai utk mode percent
    free_margin: Optional[float] = None           # dipakai utk validasi margin_needed

    # ── Kegagalan & catatan ──────────────────────────────────────────────
    failure_reason: Optional[str] = None
    # 'invalid_sl_distance' | 'sl_distance_too_tight' | 'sl_wrong_side' |
    # 'entry_price_deviation_too_large' | 'missing_entry_price' |
    # 'insufficient_margin' | 'exchange_error' | None (kalau success=True)
    notes: list = field(default_factory=list)

    def recompute_margin(self, leverage_used: float) -> None:
        """
        Recompute `margin_needed` dengan `leverage_used` baru — dipanggil oleh
        leverage_engine (Step 10) SETELAH safety check liquidation vs SL.

        TIDAK mengubah risk_amount_usd atau position_size — leverage hanya
        boleh mengubah margin_needed, sesuai bagian 4.4.
        """
        if self.position_size is None or self.entry_price_used is None:
            raise ValueError(
                "recompute_margin: position_size/entry_price_used belum terisi — "
                "panggil calculate_trade_risk() dulu."
            )
        self.leverage_used = leverage_used
        self.margin_needed = calculate_margin_needed(
            self.position_size, self.entry_price_used, leverage_used
        )

    def notification_text(self) -> str:
        """Format notifikasi singkat yang TEGAS membedakan max_loss vs margin."""
        return format_risk_notification(self)


# ── Fungsi murni (bagian 4.4) — tidak ada I/O ────────────────────────────

def calculate_risk_amount(
    risk_mode: str,
    risk_value: float,
    total_balance: Optional[float] = None,
) -> float:
    """
    Hitung risk_amount (= kerugian aktual jika SL hit) sesuai mode aktif.

    Bagian 4.1:
      Percent   : risk_amount = total_balance * (risk_value / 100)
      Fixed USD : risk_amount = risk_value (tetap, TIDAK bergantung balance)

    Args:
        risk_mode     : RiskMode.PERCENT atau RiskMode.FIXED_USD
        risk_value    : persen (mode percent, mis. 1.0 = 1%) ATAU nominal USD
                         tetap (mode fixed_usd, mis. 5.0 = $5)
        total_balance : total equity akun — WAJIB diisi untuk mode percent,
                         diabaikan untuk mode fixed_usd

    Raises:
        ValueError: risk_value <= 0, atau mode percent tanpa total_balance,
                    atau total_balance <= 0
    """
    if risk_value <= 0:
        raise ValueError(f"risk_value harus > 0, dapat: {risk_value}")

    if risk_mode == RiskMode.PERCENT:
        if total_balance is None or total_balance <= 0:
            raise ValueError(
                f"Mode percent butuh total_balance > 0, dapat: {total_balance}"
            )
        return total_balance * (risk_value / 100.0)

    if risk_mode == RiskMode.FIXED_USD:
        # risk_value adalah nominal USD tetap — TIDAK diskalakan dari balance
        # apapun yang terjadi (lihat bagian 4.1, command /setmaxloss).
        return risk_value

    raise ValueError(f"risk_mode tidak dikenal: '{risk_mode}'")


def calculate_sl_distance(entry_price: float, sl_price: float) -> float:
    """
    Hitung jarak absolut antara entry & stop loss.

    Raises:
        ValueError: entry_price/sl_price <= 0, atau entry_price == sl_price
                    (jarak nol — tidak ada cara menghitung position_size yang
                    valid dari risk_amount / 0).
    """
    if entry_price <= 0 or sl_price <= 0:
        raise ValueError(
            f"entry_price & sl_price harus > 0, dapat: "
            f"entry={entry_price}, sl={sl_price}"
        )
    distance = abs(entry_price - sl_price)
    if distance <= 0:
        raise ValueError(
            "sl_distance = 0 — entry_price dan sl_price tidak boleh sama "
            "(tidak ada cara menghitung position_size yang valid)."
        )
    return distance


def calculate_default_tp_price(
    direction: str,
    entry_price: float,
    sl_price: float,
    rr: float = 2.0,
) -> float:
    """
    Hitung TP default berbasis Risk:Reward — dipakai kalau sinyal TIDAK
    mencantumkan harga TP (mayoritas sinyal memang tidak pernah kasih TP,
    hanya entry + SL). Default RR 1:2: jarak TP dari entry = 2x jarak SL
    dari entry, di sisi yang menguntungkan sesuai arah posisi.

    LONG  -> TP = entry + rr * sl_distance (di atas entry)
    SHORT -> TP = entry - rr * sl_distance (di bawah entry)
    """
    sl_distance = calculate_sl_distance(entry_price, sl_price)
    if direction == Direction.LONG:
        return entry_price + rr * sl_distance
    return entry_price - rr * sl_distance


def calculate_position_size(risk_amount: float, sl_distance: float) -> float:
    """
    Hitung position_size (unit aset) — bagian 4.4 langkah 2.

    position_size = risk_amount / sl_distance

    PENTING: leverage TIDAK PERNAH masuk ke formula ini. position_size murni
    fungsi dari risk_amount (kebijakan risk) dan jarak SL (bagian 4.4: "leverage
    tidak pernah mengubah position_size atau besar kerugian maksimal").

    Raises:
        ValueError: risk_amount <= 0 atau sl_distance <= 0
    """
    if risk_amount <= 0:
        raise ValueError(f"risk_amount harus > 0, dapat: {risk_amount}")
    if sl_distance <= 0:
        raise ValueError(f"sl_distance harus > 0, dapat: {sl_distance}")
    return risk_amount / sl_distance


def calculate_margin_needed(
    position_size: float,
    entry_price: float,
    leverage_used: float,
) -> float:
    """
    Hitung margin_needed (margin yang dikunci exchange) — bagian 4.4 langkah 3.

    margin_needed = (position_size * entry_price) / leverage_used

    PENTING: ini SATU-SATUNYA tempat leverage berpengaruh ke perhitungan.
    Margin akan berbeda-beda tiap trade — itu wajar (lihat bagian 4.4).

    Raises:
        ValueError: salah satu parameter <= 0
    """
    if position_size <= 0:
        raise ValueError(f"position_size harus > 0, dapat: {position_size}")
    if entry_price <= 0:
        raise ValueError(f"entry_price harus > 0, dapat: {entry_price}")
    if leverage_used <= 0:
        raise ValueError(f"leverage_used harus > 0, dapat: {leverage_used}")
    return (position_size * entry_price) / leverage_used


def resolve_leverage_used(
    max_leverage_available: float,
    leverage_cap: Optional[float],
) -> tuple[float, bool]:
    """
    Tentukan leverage_used DEFAULT sebelum safety check Step 10.

    Bagian 4.2: "Default behavior: leverage_used = max_leverage_for_symbol,
    KECUALI user sudah set cap manual lewat /setleverage."

    Return:
        (leverage_used, capped_by_user)
        capped_by_user = True jika leverage_cap < max_leverage_available
        (artinya angka yang dipakai adalah hasil cap user, bukan max asli)
    """
    if max_leverage_available <= 0:
        raise ValueError(
            f"max_leverage_available harus > 0, dapat: {max_leverage_available}"
        )
    if leverage_cap is not None and leverage_cap > 0 and leverage_cap < max_leverage_available:
        return leverage_cap, True
    return max_leverage_available, False


# ── Orchestrator async — melakukan I/O (balance, leverage, ticker) ──────

async def calculate_trade_risk(
    *,
    pair: str,
    direction: str,
    entry_type: str,
    entry_price: Optional[float],
    sl_price: float,
    rest_client: Optional[BitgetRestClient] = None,
) -> RiskCalculationResult:
    """
    Hitung risk & margin lengkap untuk satu kandidat trade — entry point
    utama modul ini, dipakai pipeline (Step 19) setelah sinyal lolos parser
    (Step 4/5) dan SEBELUM leverage safety check (Step 10) & eksekusi
    (Step 12).

    Args:
        pair        : unified symbol Bitget (mis. "STG/USDT:USDT") — hasil
                      `ParsedSignal.pair_normalized`
        direction   : Direction.LONG atau Direction.SHORT — dipakai untuk
                      validasi sisi SL (LONG wajib sl < entry, SHORT wajib
                      sl > entry). Sinyal dengan SL di sisi yang salah
                      hampir selalu berarti salah baca harga (typo/kurang
                      digit) — lihat validasi di bawah.
        entry_type  : EntryType.LIMIT atau EntryType.MARKET
        entry_price : harga entry dari sinyal. WAJIB untuk limit. Untuk
                      market, BOLEH None (sinyal "Entry market" tanpa harga
                      eksplisit) — kalau None, harga current diambil dari
                      ticker REST sebagai estimasi.
        sl_price    : harga stop loss dari sinyal (WAJIB, sudah divalidasi
                      ada oleh parser)
        rest_client : override client (untuk unit test / DI). Default pakai
                      singleton `get_rest_client()`.

    Returns:
        RiskCalculationResult — `success=False` untuk SEMUA kegagalan
        (input tidak valid, saldo tidak cukup, error exchange) — TIDAK ADA
        exception yang bocor ke caller untuk kasus-kasus ini, supaya
        pipeline bisa langsung kirim notifikasi & berhenti dengan rapi
        tanpa try/except bertingkat. Lihat `failure_reason` untuk detail.
    """
    client = rest_client or get_rest_client()
    result = RiskCalculationResult(success=False, sl_price=sl_price)

    # ── 1. Resolve entry_price efektif (handle "Entry market" tanpa harga) ─
    try:
        if entry_price is not None and entry_price > 0:
            effective_entry_price = entry_price
            result.entry_price_estimated = False
        elif entry_type == EntryType.MARKET:
            effective_entry_price = await client.fetch_ticker_price(pair)
            result.entry_price_estimated = True
            result.notes.append(
                f"entry_price tidak eksplisit di sinyal (order market) — "
                f"dipakai estimasi harga ticker terkini: {effective_entry_price:.8f}. "
                f"Harga fill aktual bisa sedikit berbeda."
            )
        else:
            result.failure_reason = "missing_entry_price"
            result.notes.append(
                "Entry type 'limit' tapi entry_price tidak ada — seharusnya "
                "sudah ditolak parser (Step 4/5). Cek pipeline upstream."
            )
            logger.error(
                "[risk_engine] missing_entry_price untuk %s — entry_type=%s",
                pair, entry_type,
            )
            return result
    except (CriticalError, TransientError) as exc:
        result.failure_reason = "exchange_error"
        result.notes.append(f"Gagal fetch ticker price untuk '{pair}': {exc}")
        logger.error("[risk_engine] %s", result.notes[-1])
        return result

    result.entry_price_used = effective_entry_price

    # ── 2. Jarak SL ──────────────────────────────────────────────────────
    try:
        sl_distance = calculate_sl_distance(effective_entry_price, sl_price)
    except ValueError as exc:
        result.failure_reason = "invalid_sl_distance"
        result.notes.append(str(exc))
        logger.warning(
            "[risk_engine] invalid_sl_distance untuk %s — entry=%s sl=%s — %s",
            pair, effective_entry_price, sl_price, exc,
        )
        return result
    result.sl_distance = sl_distance

    # Jarak SL nol sudah ditolak calculate_sl_distance() di atas — tapi jarak
    # yang NON-ZERO namun terlalu sempit sama berbahayanya: position_size =
    # risk_amount / sl_distance akan membengkak tidak wajar (lihat docstring
    # modul), bikin notional jauh lebih besar dari yang masuk akal untuk
    # akun manapun — leverage TIDAK BISA memperbaiki ini (leverage cuma
    # mengubah margin_needed, bukan position_size). Ditolak DI SINI, sebelum
    # sampai ke leverage_engine/exchange, dengan alasan yang jelas.
    sl_distance_pct = sl_distance / effective_entry_price
    if sl_distance_pct < settings.MIN_SL_DISTANCE_PERCENT:
        result.failure_reason = "sl_distance_too_tight"
        result.notes.append(
            f"Jarak SL ke entry cuma {sl_distance_pct * 100:.4f}% "
            f"(minimum {settings.MIN_SL_DISTANCE_PERCENT * 100:.2f}%) — "
            f"entry={effective_entry_price:g}, sl={sl_price:g}. Kemungkinan "
            f"besar salah baca/salah ketik harga SL dari sinyal. Kalaupun "
            f"benar disengaja, SL sesempit ini akan membuat position_size "
            f"membengkak sampai TIDAK ADA leverage yang bisa membuatnya "
            f"aman dari liquidation — trade ini ditolak, bukan dipaksakan."
        )
        logger.warning(
            "[risk_engine] sl_distance_too_tight untuk %s — %.4f%% < min %.2f%% "
            "(entry=%s sl=%s)",
            pair, sl_distance_pct * 100, settings.MIN_SL_DISTANCE_PERCENT * 100,
            effective_entry_price, sl_price,
        )
        return result

    # ── 2b. SL harus di sisi yang benar dari entry sesuai direction ──────
    # LONG  → SL WAJIB di bawah entry (rugi kalau harga turun)
    # SHORT → SL WAJIB di atas entry (rugi kalau harga naik)
    # SL di sisi yang salah HAMPIR SELALU berarti salah baca angka dari
    # sinyal (kurang/lebih digit, salah kolom entry vs SL, dsb) — bukan
    # setup trading yang valid. Kalau dipaksa jalan, sl_distance yang
    # terhitung jadi tidak berarti apa-apa (bisa ratusan persen dari
    # entry), bikin position_size anjlok di bawah minimum order exchange
    # (persis kasus entry=6834 vs sl=63750 untuk LONG — sl_distance
    # kehitung 56916, padahal itu bukan "SL jauh", itu "harga entry salah
    # baca").
    if direction == Direction.LONG and sl_price >= effective_entry_price:
        result.failure_reason = "sl_wrong_side"
        result.notes.append(
            f"Sinyal LONG tapi SL ({sl_price:g}) >= entry ({effective_entry_price:g}) "
            f"— untuk LONG, SL wajib di BAWAH entry. Kemungkinan besar salah "
            f"baca harga entry atau SL dari sinyal (cek digit yang mungkin "
            f"kurang/tertukar). Trade ditolak."
        )
        logger.warning(
            "[risk_engine] sl_wrong_side (LONG) untuk %s — entry=%s sl=%s",
            pair, effective_entry_price, sl_price,
        )
        return result
    if direction == Direction.SHORT and sl_price <= effective_entry_price:
        result.failure_reason = "sl_wrong_side"
        result.notes.append(
            f"Sinyal SHORT tapi SL ({sl_price:g}) <= entry ({effective_entry_price:g}) "
            f"— untuk SHORT, SL wajib di ATAS entry. Kemungkinan besar salah "
            f"baca harga entry atau SL dari sinyal (cek digit yang mungkin "
            f"kurang/tertukar). Trade ditolak."
        )
        logger.warning(
            "[risk_engine] sl_wrong_side (SHORT) untuk %s — entry=%s sl=%s",
            pair, effective_entry_price, sl_price,
        )
        return result

    # ── 2c. Sanity check entry_price vs harga pasar live ──────────────────
    # Menangkap kasus lain yang sl_wrong_side TIDAK selalu tangkap: entry
    # kurang/lebih digit tapi kebetulan masih di sisi yang "benar" dari SL.
    # Kalau entry_price (dari sinyal, bukan hasil estimasi ticker di atas —
    # itu sudah pasti live) menyimpang > MAX_ENTRY_PRICE_DEVIATION_PERCENT
    # dari harga pasar saat ini, kemungkinan besar salah baca, bukan limit
    # order yang jauh dari market secara sengaja.
    if not result.entry_price_estimated:
        try:
            live_price = await client.fetch_ticker_price(pair)
            deviation_pct = abs(effective_entry_price - live_price) / live_price
            if deviation_pct > settings.MAX_ENTRY_PRICE_DEVIATION_PERCENT:
                result.failure_reason = "entry_price_deviation_too_large"
                result.notes.append(
                    f"Harga entry sinyal ({effective_entry_price:g}) menyimpang "
                    f"{deviation_pct * 100:.1f}% dari harga pasar saat ini "
                    f"({live_price:g}) — lebih dari batas "
                    f"{settings.MAX_ENTRY_PRICE_DEVIATION_PERCENT * 100:.0f}%. "
                    f"Kemungkinan besar salah baca angka entry dari sinyal "
                    f"(kurang/lebih digit). Trade ditolak — cek ulang sinyal "
                    f"asli kalau memang sengaja entry sejauh ini dari market."
                )
                logger.warning(
                    "[risk_engine] entry_price_deviation_too_large untuk %s — "
                    "entry=%s live=%s deviasi=%.1f%%",
                    pair, effective_entry_price, live_price, deviation_pct * 100,
                )
                return result
        except (CriticalError, TransientError) as exc:
            # Sanity check gagal fetch bukan alasan buat block trade yang
            # sudah lolos validasi lain — cukup dicatat sebagai note.
            result.notes.append(
                f"Sanity check harga entry vs live market dilewati "
                f"(gagal fetch ticker: {exc})."
            )
            logger.warning(
                "[risk_engine] Gagal fetch live price untuk sanity check %s: %s",
                pair, exc,
            )

    # ── 3. risk_amount sesuai mode aktif (bagian 4.1) ───────────────────
    try:
        risk_mode, risk_value = await async_get_risk_amount_config()
    except Exception as exc:  # noqa: BLE001 — settings read tidak boleh crash risk engine
        result.failure_reason = "exchange_error"
        result.notes.append(f"Gagal baca konfigurasi risk dari settings: {exc}")
        logger.error("[risk_engine] %s", result.notes[-1])
        return result

    result.risk_mode = risk_mode

    try:
        balance = await client.fetch_balance()
    except (CriticalError, TransientError) as exc:
        result.failure_reason = "exchange_error"
        result.notes.append(f"Gagal fetch balance akun: {exc}")
        logger.error("[risk_engine] %s", result.notes[-1])
        return result

    result.total_equity = balance.total_equity
    result.free_margin = balance.free_margin

    try:
        if risk_mode == RiskMode.PERCENT:
            risk_amount = calculate_risk_amount(
                risk_mode, risk_value, total_balance=balance.total_equity
            )
            result.risk_percent_used = risk_value
        else:
            risk_amount = calculate_risk_amount(risk_mode, risk_value)
            result.risk_percent_used = None
    except ValueError as exc:
        result.failure_reason = "invalid_risk_config"
        result.notes.append(str(exc))
        logger.error("[risk_engine] invalid_risk_config untuk %s — %s", pair, exc)
        return result

    result.risk_amount_usd = risk_amount

    # ── 4. position_size — TIDAK dipengaruhi leverage (bagian 4.4 #2) ───
    try:
        position_size = calculate_position_size(risk_amount, sl_distance)
    except ValueError as exc:
        result.failure_reason = "invalid_position_size"
        result.notes.append(str(exc))
        logger.error("[risk_engine] invalid_position_size untuk %s — %s", pair, exc)
        return result
    result.position_size = position_size

    # ── 5. leverage_used DEFAULT (Step 10 akan menyesuaikan demi safety) ─
    try:
        max_leverage = await client.get_max_leverage(pair)
    except (CriticalError, TransientError) as exc:
        result.failure_reason = "exchange_error"
        result.notes.append(f"Gagal fetch max leverage untuk '{pair}': {exc}")
        logger.error("[risk_engine] %s", result.notes[-1])
        return result
    result.max_leverage_available = max_leverage

    leverage_cap = await async_get_leverage_cap(pair)
    leverage_used, capped_by_user = resolve_leverage_used(max_leverage, leverage_cap)
    result.leverage_used = leverage_used
    result.leverage_capped_by_user = capped_by_user
    if capped_by_user:
        result.notes.append(
            f"Leverage dibatasi manual via /setleverage ke {leverage_used:.0f}x "
            f"(max asli exchange: {max_leverage:.0f}x)."
        )

    # ── 6. margin_needed — SATU-SATUNYA tempat leverage berpengaruh ─────
    try:
        margin_needed = calculate_margin_needed(
            position_size, effective_entry_price, leverage_used
        )
    except ValueError as exc:
        result.failure_reason = "invalid_margin"
        result.notes.append(str(exc))
        logger.error("[risk_engine] invalid_margin untuk %s — %s", pair, exc)
        return result
    result.margin_needed = margin_needed

    # ── 7. Validasi saldo fisik — bagian 4.4: "margin_needed <= available_balance" ─
    # CATATAN: ini validasi TERPISAH dari risk_amount (bagian 4.1) — risk_amount
    # adalah kebijakan risk, margin_needed<=free_margin adalah cek fisik saldo.
    # JANGAN dicampur (lihat bagian 4.1 baris terakhir).
    if margin_needed > balance.free_margin:
        result.failure_reason = "insufficient_margin"
        result.notes.append(
            f"margin_needed ({margin_needed:.4f} USDT) > free_margin tersedia "
            f"({balance.free_margin:.4f} USDT) — trade DIBATALKAN."
        )
        logger.warning(
            "[risk_engine] insufficient_margin untuk %s — needed=%.4f free=%.4f",
            pair, margin_needed, balance.free_margin,
        )
        return result

    result.success = True
    result.failure_reason = None
    logger.info(
        "[risk_engine] %s | risk_amount=%.4f USDT (mode=%s) | position_size=%.8f | "
        "leverage=%.0fx | margin_needed=%.4f USDT | free_margin=%.4f USDT",
        pair, result.risk_amount_usd, result.risk_mode, result.position_size,
        result.leverage_used, result.margin_needed, result.free_margin,
    )
    return result


# ── Notifikasi ────────────────────────────────────────────────────────────

def format_risk_notification(result: RiskCalculationResult) -> str:
    """
    Format notifikasi singkat untuk Telegram — TEGAS membedakan risk_amount
    (max loss aktual jika SL hit) vs margin_needed (margin yang dikunci
    exchange), sesuai bagian 6 & 7 prompt.md.

    Dipakai oleh pipeline (Step 19) / notifications module untuk membentuk
    pesan sebelum eksekusi, atau sebagai bagian dari alert kegagalan.
    """
    if not result.success:
        reason_text = {
            "invalid_sl_distance": "Entry price = SL price (jarak nol) — sinyal tidak valid.",
            "sl_distance_too_tight": "Jarak SL ke entry terlalu sempit — position size akan membengkak tidak wajar & tidak ada leverage yang bisa membuatnya aman dari liquidation.",
            "sl_wrong_side": "SL berada di sisi yang salah dari entry untuk arah trade ini (kemungkinan salah baca harga).",
            "entry_price_deviation_too_large": "Harga entry menyimpang terlalu jauh dari harga pasar live (kemungkinan salah baca harga).",
            "missing_entry_price": "Harga entry tidak tersedia untuk order limit.",
            "insufficient_margin": "Margin yang dibutuhkan lebih besar dari saldo tersedia.",
            "invalid_risk_config": "Konfigurasi risk (mode/persen/fixed USD) tidak valid.",
            "invalid_position_size": "Gagal menghitung position size.",
            "invalid_margin": "Gagal menghitung margin yang dibutuhkan.",
            "exchange_error": "Gagal terhubung ke Bitget (balance/leverage/ticker).",
        }.get(result.failure_reason, "Alasan tidak diketahui.")

        lines = [f"❌ Risk engine: trade DIBATALKAN — {reason_text}"]
        if result.risk_amount_usd:
            lines.append(f"   • Risk amount (max loss): {result.risk_amount_usd:.2f} USDT")
        if result.margin_needed is not None:
            lines.append(f"   • Margin yang dibutuhkan: {result.margin_needed:.2f} USDT")
        if result.free_margin is not None:
            lines.append(f"   • Free margin tersedia: {result.free_margin:.2f} USDT")
        if result.notes:
            lines.append(f"   • Detail: {result.notes[-1]}")
        return "\n".join(lines)

    mode_label = (
        f"{result.risk_percent_used:.2f}% dari total balance"
        if result.risk_mode == RiskMode.PERCENT
        else "Fixed USD"
    )

    lines = [
        "✅ Risk & margin terhitung:",
        f"   • Max loss jika SL hit: {result.risk_amount_usd:.2f} USDT  (mode: {mode_label})",
        f"   • Margin yang akan dikunci exchange: {result.margin_needed:.2f} USDT "
        f"(leverage {result.leverage_used:.0f}x)",
        f"   • Position size: {result.position_size:.8f} unit",
    ]
    if result.leverage_capped_by_user:
        lines.append(
            f"   • ℹ️ Leverage dibatasi manual ke {result.leverage_used:.0f}x "
            f"(max exchange: {result.max_leverage_available:.0f}x)"
        )
    if result.entry_price_estimated:
        lines.append(
            f"   • ℹ️ Entry market tanpa harga eksplisit — dipakai estimasi "
            f"ticker: {result.entry_price_used:.8f} (harga fill aktual bisa beda)"
        )
    lines.append(
        "   ⚠️ Catatan: margin di atas BUKAN angka kerugian — kerugian aktual "
        "jika SL hit tetap persis sebesar max loss di atas."
    )
    return "\n".join(lines)