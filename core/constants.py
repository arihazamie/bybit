"""
core/constants.py
=================
Konstanta global yang dipakai di seluruh komponen bot.
Tidak ada logika di sini — murni definisi nilai tetap.
"""

import pytz

# ── Timezone ────────────────────────────────────────────────────────────
TZ_UTC = pytz.utc
TZ_JAKARTA = pytz.timezone("Asia/Jakarta")

# ── Circuit Breaker — nama komponen ────────────────────────────────────
class Component:
    """Nama komponen yang punya circuit breaker masing-masing."""
    TELEGRAM_LISTENER = "telegram_listener"
    BITGET_CONNECTION  = "bitget_connection"
    ORDER_EXECUTION    = "order_execution"
    SIGNAL_PARSER      = "signal_parser"

    ALL = (
        TELEGRAM_LISTENER,
        BITGET_CONNECTION,
        ORDER_EXECUTION,
        SIGNAL_PARSER,
    )


# ── Circuit Breaker — state ─────────────────────────────────────────────
class CBState:
    """State machine circuit breaker: CLOSED → OPEN → HALF_OPEN → CLOSED."""
    CLOSED    = "closed"      # Normal, operasi jalan
    OPEN      = "open"        # Trip — eksekusi berhenti, tunggu /resume
    HALF_OPEN = "half_open"   # Sedang dicoba satu operasi test setelah /resume


# ── Bot mode ────────────────────────────────────────────────────────────
class BotMode:
    """Mode operasi bot saat ini."""
    PAUSED           = "paused"            # /pause aktif — tidak eksekusi sinyal baru
    RUNNING          = "running"           # Normal
    CIRCUIT_TRIPPED  = "circuit_tripped"   # Circuit breaker trip — beda dari /pause


# ── Risk mode ───────────────────────────────────────────────────────────
class RiskMode:
    """Mode kalkulasi max loss per trade."""
    PERCENT   = "percent"    # risk = X% dari total balance
    FIXED_USD = "fixed_usd"  # risk = nominal USD tetap


# ── Conflict mode ───────────────────────────────────────────────────────
class ConflictMode:
    """Perilaku saat sinyal baru masuk untuk pair yang sudah ada posisi/order."""
    ASK     = "ask"      # Tanya user via inline button (default)
    SKIP    = "skip"     # Abaikan sinyal baru otomatis
    ADD     = "add"      # Buka posisi tambahan tanpa tanya
    REPLACE = "replace"  # Cancel posisi/order lama, buka yang baru


# ── Trade status ────────────────────────────────────────────────────────
class TradeStatus:
    PENDING   = "pending"    # Limit order belum fill
    OPEN      = "open"       # Posisi sedang berjalan
    CLOSED    = "closed"     # Posisi sudah ditutup
    CANCELLED = "cancelled"  # Order dibatalkan sebelum fill


# ── Trade direction ─────────────────────────────────────────────────────
class Direction:
    LONG  = "long"
    SHORT = "short"


# ── Entry type ──────────────────────────────────────────────────────────
class EntryType:
    LIMIT  = "limit"
    MARKET = "market"


# ── Close reason ────────────────────────────────────────────────────────
class CloseReason:
    SL_HIT      = "sl_hit"
    TP_HIT      = "tp_hit"
    MANUAL      = "manual_close"
    LIQUIDATED  = "liquidated"


# ── Signal parse status ─────────────────────────────────────────────────
class ParseStatus:
    SUCCESS   = "success"
    AMBIGUOUS = "ambiguous"
    INFO_ONLY = "info_only"   # pesan update (hit entry, Close NR, dll.)
    INVALID   = "invalid"


# ── Message type (klasifikasi awal sebelum field extraction) ────────────
class MessageType:
    """Hasil klasifikasi pesan masuk — dipakai Step 5 ambiguity engine."""
    NEW_SIGNAL_CANDIDATE = "new_signal_candidate"  # kandidat sinyal entry baru
    INFO_HIT_ENTRY        = "info_hit_entry"        # "{PAIR} hit entry"
    INFO_CLOSE            = "info_close"            # "{PAIR} Close NR" / "manual close NR"
    INFO_RUNNING          = "info_running"          # "{PAIR} running NR"
    UNKNOWN               = "unknown"               # tidak match pola apapun


# ── Info event type (subtipe pesan non-entry) ────────────────────────────
class InfoEventType:
    """Subtipe event informational — dipetakan dari MessageType.INFO_*."""
    HIT_ENTRY    = "hit_entry"
    CLOSE        = "close"
    MANUAL_CLOSE = "manual_close"
    RUNNING      = "running"


# ── Event severity ──────────────────────────────────────────────────────
class Severity:
    INFO     = "info"
    WARNING  = "warning"
    CRITICAL = "critical"


# ── Event type ──────────────────────────────────────────────────────────
class EventType:
    CIRCUIT_BREAKER_TRIP  = "circuit_breaker_trip"
    CIRCUIT_BREAKER_RESET = "circuit_breaker_reset"
    LEVERAGE_ADJUSTED     = "leverage_adjusted"
    POSITION_CONFLICT     = "position_conflict"
    LIQUIDATION_WARNING   = "liquidation_warning"
    OTHER                 = "other"


# ── Retry backoff (detik) ───────────────────────────────────────────────
RETRY_BACKOFF_SECONDS = (2, 5, 15)   # percobaan ke-1, ke-2, ke-3

# ── WebSocket reconciliation interval ──────────────────────────────────
WS_RECONCILE_INTERVAL_SECONDS = 60   # fallback REST polling tiap 1 menit

# ── Inline button timeout (menit) ──────────────────────────────────────
INLINE_BUTTON_TIMEOUT_MINUTES = 10   # setelah ini → auto-abaikan sinyal ambigu

# ── Margin mode ────────────────────────────────────────────────────────
MARGIN_MODE = "cross"   # Keputusan final: semua posisi pakai cross margin

# ── Position condition (Step 11 — position checker) ──────────────────────
class PositionCondition:
    """
    Kondisi pair saat ini sebelum eksekusi sinyal baru — gabungan data live
    Bitget (fetch_positions / fetch_open_orders) + database lokal.
    """
    NONE              = "none"               # tidak ada posisi/pending → eksekusi normal
    OPEN_POSITION     = "open_position"       # sudah ada posisi open
    PENDING_ORDER     = "pending_order"       # ada limit order belum fill
    OPEN_AND_PENDING  = "open_and_pending"    # ada posisi open DAN pending order (kasus jarang)


# ── Position conflict action (Step 11) ────────────────────────────────────
class PositionAction:
    """Aksi yang direkomendasikan position_checker untuk sinyal baru."""
    PROCEED           = "proceed"             # tidak ada konflik → lanjut eksekusi normal
    ASK_CONFIRMATION  = "ask_confirmation"    # conflict_mode=ask → tunggu user (Step 18)
    SKIP              = "skip"                # conflict_mode=skip → abaikan sinyal baru
    ADD               = "add"                 # conflict_mode=add → buka posisi tambahan
    REPLACE           = "replace"             # conflict_mode=replace → cancel/close lama, buka baru
