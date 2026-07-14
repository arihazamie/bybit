"""
db/models.py
SQLite schema definitions (SQL DDL) untuk semua tabel.
Semua tabel didefinisikan di sini sebagai konstanta SQL CREATE statement.
"""

# ─────────────────────────────────────────────
# TABLE: trades
# Histori semua trade yang dieksekusi bot
# ─────────────────────────────────────────────
CREATE_TRADES_TABLE = """
CREATE TABLE IF NOT EXISTS trades (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,

    -- Identitas pair & arah
    pair                        TEXT NOT NULL,
    direction                   TEXT NOT NULL CHECK(direction IN ('long', 'short')),
    entry_type                  TEXT NOT NULL CHECK(entry_type IN ('limit', 'market')),

    -- Harga
    entry_price                 REAL NOT NULL,
    sl_price                    REAL NOT NULL,
    sl_order_id                 TEXT,           -- order id SL aktif di exchange (dipakai utk cancel saat /setsl update)
    tp_price                    REAL,           -- nullable; diisi manual oleh user
    tp_order_id                 TEXT,           -- order id TPSL take-profit aktif di exchange (dipakai utk cancel saat /settp update)

    -- Ukuran posisi & margin
    position_size               REAL NOT NULL,
    margin_used                 REAL,           -- diisi setelah order fill

    -- Risk
    risk_mode                   TEXT NOT NULL CHECK(risk_mode IN ('percent', 'fixed_usd')),
    risk_amount_usd             REAL NOT NULL,
    risk_percent_used           REAL,           -- nullable; terisi jika mode=percent

    -- Leverage
    max_leverage_available      REAL,
    leverage_used               REAL,
    leverage_auto_adjusted      INTEGER NOT NULL DEFAULT 0 CHECK(leverage_auto_adjusted IN (0, 1)),

    -- Liquidation estimate
    liquidation_price_estimate  REAL,

    -- Status & timeline
    status                      TEXT NOT NULL DEFAULT 'pending'
                                    CHECK(status IN ('pending', 'open', 'closed', 'cancelled')),
    opened_at                   TEXT,           -- ISO8601 UTC
    closed_at                   TEXT,           -- ISO8601 UTC; nullable
    close_reason                TEXT CHECK(close_reason IN
                                    ('sl_hit', 'tp_hit', 'manual_close', 'liquidated', NULL)),

    -- Hasil
    pnl                         REAL,
    r_multiple                  REAL,

    -- Metadata sinyal
    raw_signal_text             TEXT,
    source_analyst              TEXT,           -- nama analyst (wush, Faith, talon, dll)
    source_message_id           INTEGER,        -- Telegram message ID sumber sinyal

    -- Conflict handling
    conflict_action_taken       TEXT,           -- skip/add/replace/ask_confirmed

    -- Timestamps
    created_at                  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at                  TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

# ─────────────────────────────────────────────
# TABLE: settings
# Key-value store untuk konfigurasi bot
# ─────────────────────────────────────────────
CREATE_SETTINGS_TABLE = """
CREATE TABLE IF NOT EXISTS settings (
    key         TEXT PRIMARY KEY NOT NULL,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

# Default settings yang di-seed saat inisialisasi DB
DEFAULT_SETTINGS = {
    "risk_mode":            "percent",      # 'percent' atau 'fixed_usd'
    "risk_percent":         "1.0",          # default 1% per trade
    "max_loss_usd":         "5.0",          # default $5 (hanya aktif jika mode=fixed_usd)
    "auto_execute_mode":    "false",        # false = perlu konfirmasi; true = langsung eksekusi
    "bot_paused":           "true",         # mulai dalam mode paused (sesuai spec)
    "position_conflict_mode": "ask",        # skip/ask/add/replace
    "cb_error_threshold":   "3",            # N error untuk trip circuit breaker
    "cb_window_minutes":    "5",            # T window waktu circuit breaker
    "liquidation_buffer_pct": "5.0",        # buffer aman liquidation vs SL (%)
    "default_leverage_cap": "",             # kosong = tidak ada cap; isi angka untuk limit global
}

# ─────────────────────────────────────────────
# TABLE: signal_log
# Log semua pesan yang masuk dari Telegram grup sinyal
# ─────────────────────────────────────────────
CREATE_SIGNAL_LOG_TABLE = """
CREATE TABLE IF NOT EXISTS signal_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id      INTEGER NOT NULL,           -- Telegram message ID; UNIQUE hanya per-chat, bukan global
    chat_id         INTEGER NOT NULL DEFAULT 0,
    sender_username TEXT,
    raw_text        TEXT NOT NULL,
    received_at     TEXT NOT NULL,              -- ISO8601 UTC

    -- Hasil parsing
    parsed_status   TEXT NOT NULL
                        CHECK(parsed_status IN ('success', 'ambiguous', 'info_only', 'error')),
    parsed_data     TEXT,                       -- JSON string hasil parse (nullable kalau gagal)
    ambiguity_reasons TEXT,                     -- JSON array alasan ambiguitas (nullable)

    -- Aksi yang diambil
    action_taken    TEXT,                       -- 'executed', 'skipped', 'awaiting_confirmation',
                                                --  'confirmed_execute', 'confirmed_skip', 'info_logged'
    trade_id        INTEGER REFERENCES trades(id),  -- nullable; diisi jika trade berhasil dibuat

    created_at      TEXT NOT NULL DEFAULT (datetime('now')),

    -- Idempotency: message_id Telegram cuma unik per-chat. Kalau listen >1
    -- grup, message_id BISA bentrok antar chat_id berbeda — UNIQUE harus
    -- gabungan (chat_id, message_id), bukan message_id sendirian.
    UNIQUE(chat_id, message_id)
);
"""

# Index untuk idempotency check (O(1) lookup by chat_id + message_id)
CREATE_SIGNAL_LOG_INDEX = """
CREATE INDEX IF NOT EXISTS idx_signal_log_chat_message ON signal_log(chat_id, message_id);
"""

# ─────────────────────────────────────────────
# TABLE: circuit_breaker_state
# State machine per komponen circuit breaker
# ─────────────────────────────────────────────
CREATE_CIRCUIT_BREAKER_TABLE = """
CREATE TABLE IF NOT EXISTS circuit_breaker_state (
    component               TEXT PRIMARY KEY NOT NULL
                                CHECK(component IN (
                                    'telegram_listener',
                                    'bitget_connection',
                                    'order_execution',
                                    'signal_parser'
                                )),
    state                   TEXT NOT NULL DEFAULT 'closed'
                                CHECK(state IN ('closed', 'open', 'half_open')),
    consecutive_error_count INTEGER NOT NULL DEFAULT 0,
    last_error_message      TEXT,
    last_error_at           TEXT,               -- ISO8601 UTC; nullable
    opened_at               TEXT,               -- ISO8601 UTC; nullable (kapan trip)
    updated_at              TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

# Seed untuk circuit breaker (semua komponen mulai CLOSED)
DEFAULT_CIRCUIT_BREAKER_COMPONENTS = [
    "telegram_listener",
    "bitget_connection",
    "order_execution",
    "signal_parser",
]

# ─────────────────────────────────────────────
# TABLE: event_log
# Log kejadian penting untuk audit trail
# ─────────────────────────────────────────────
CREATE_EVENT_LOG_TABLE = """
CREATE TABLE IF NOT EXISTS event_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type  TEXT NOT NULL
                    CHECK(event_type IN (
                        'circuit_breaker_trip',
                        'circuit_breaker_reset',
                        'leverage_adjusted',
                        'position_conflict',
                        'liquidation_warning',
                        'sl_hit',
                        'tp_hit',
                        'entry_filled',
                        'order_failed',
                        'bot_paused',
                        'bot_resumed',
                        'settings_changed',
                        'other'
                    )),
    component   TEXT,           -- komponen terkait (nullable)
    message     TEXT NOT NULL,
    severity    TEXT NOT NULL DEFAULT 'info'
                    CHECK(severity IN ('info', 'warning', 'critical')),
    trade_id    INTEGER REFERENCES trades(id),  -- nullable
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

# Index pada event_log untuk query cepat per severity/type
CREATE_EVENT_LOG_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_event_log_type ON event_log(event_type);",
    "CREATE INDEX IF NOT EXISTS idx_event_log_severity ON event_log(severity);",
    "CREATE INDEX IF NOT EXISTS idx_event_log_created ON event_log(created_at DESC);",
]

# ─────────────────────────────────────────────
# Semua DDL statements dalam urutan yang benar
# ─────────────────────────────────────────────
ALL_CREATE_STATEMENTS = [
    CREATE_TRADES_TABLE,
    CREATE_SETTINGS_TABLE,
    CREATE_SIGNAL_LOG_TABLE,
    CREATE_SIGNAL_LOG_INDEX,
    CREATE_CIRCUIT_BREAKER_TABLE,
    CREATE_EVENT_LOG_TABLE,
    *CREATE_EVENT_LOG_INDEXES,
]

# Trigger untuk auto-update updated_at pada trades
CREATE_TRADES_UPDATED_AT_TRIGGER = """
CREATE TRIGGER IF NOT EXISTS trades_updated_at
AFTER UPDATE ON trades
BEGIN
    UPDATE trades SET updated_at = datetime('now') WHERE id = NEW.id;
END;
"""

# Trigger untuk auto-update updated_at pada settings
CREATE_SETTINGS_UPDATED_AT_TRIGGER = """
CREATE TRIGGER IF NOT EXISTS settings_updated_at
AFTER UPDATE ON settings
BEGIN
    UPDATE settings SET updated_at = datetime('now') WHERE key = NEW.key;
END;
"""

# Trigger untuk auto-update updated_at pada circuit_breaker_state
CREATE_CB_UPDATED_AT_TRIGGER = """
CREATE TRIGGER IF NOT EXISTS cb_updated_at
AFTER UPDATE ON circuit_breaker_state
BEGIN
    UPDATE circuit_breaker_state SET updated_at = datetime('now') WHERE component = NEW.component;
END;
"""

ALL_TRIGGERS = [
    CREATE_TRADES_UPDATED_AT_TRIGGER,
    CREATE_SETTINGS_UPDATED_AT_TRIGGER,
    CREATE_CB_UPDATED_AT_TRIGGER,
]