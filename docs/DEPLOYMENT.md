# Bitget Signal Bot — Panduan Deployment & Go-Live

> ⚠️ Trading futures dengan leverage berisiko tinggi. Selalu selesaikan pengujian di akun Demo sebelum menghubungkan API key live.

---

## Daftar Isi

1. [Prasyarat](#1-prasyarat)
2. [Setup Bitget Demo API](#2-setup-bitget-demo-api)
3. [Setup Telegram](#3-setup-telegram)
4. [Konfigurasi `.env`](#4-konfigurasi-env)
5. [Mode Operasi](#5-mode-operasi)
6. [Menjalankan Bot](#6-menjalankan-bot)
7. [Urutan Pengujian Demo](#7-urutan-pengujian-demo)
8. [Monitoring & Log](#8-monitoring--log)
9. [Command Telegram Referensi Cepat](#9-command-telegram-referensi-cepat)
10. [Checklist Go-Live (API Key Real)](#10-checklist-go-live-api-key-real)
11. [Troubleshooting](#11-troubleshooting)

---

## 1. Prasyarat

| Kebutuhan | Versi / Catatan |
|-----------|-----------------|
| Python | 3.11+ |
| OS | Linux/macOS/WSL2 (Windows native tidak direkomendasikan) |
| RAM | Minimal 512 MB |
| Koneksi internet | Stabil (WebSocket butuh koneksi persisten) |
| Akun Bitget | Demo dulu, live nanti |
| Akun Telegram | Untuk Telethon (akun pribadi) + satu bot via @BotFather |

```bash
# Cek versi Python
python3 --version   # harus 3.11+

# Buat virtual environment
python3.11 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

---

## 2. Setup Bitget Demo API

### 2.1 Aktifkan Paper Trading
1. Login ke [bitget.com](https://www.bitget.com)
2. Buka **Futures** → klik profil → **Paper Trading / Demo Trading**
3. Pastikan sudah ada saldo demo (biasanya $10,000 USDT otomatis diberikan)

### 2.2 Buat API Key Demo
1. Di halaman Paper Trading → **API Management**
2. Klik **Create API Key**
3. Centang permission: `Read`, `Trade`, `Transfer` (jangan Withdraw)
4. Simpan: `API Key`, `Secret Key`, `Passphrase`

> ⚠️ API key Demo dan Live **berbeda**. Jangan tukar-tukar.

---

## 3. Setup Telegram

### 3.1 Telethon (Listener sinyal)
Telethon butuh `api_id` dan `api_hash` dari akun Telegram pribadi:

1. Buka [my.telegram.org](https://my.telegram.org)
2. Login → **API development tools**
3. Buat app baru → catat `App api_id` dan `App api_hash`
4. Isi `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELEGRAM_PHONE` di `.env`

Saat pertama kali dijalankan, Telethon akan meminta kode OTP yang dikirim ke akun Telegram kamu.

### 3.2 Control Bot (@BotFather)
1. Chat dengan [@BotFather](https://t.me/BotFather) di Telegram
2. `/newbot` → ikuti instruksi → catat **bot token**
3. Isi `TELEGRAM_BOT_TOKEN` di `.env`

### 3.3 Chat ID untuk notifikasi
Cara mudah mendapatkan `TELEGRAM_CONTROL_CHAT_ID`:
1. Start bot yang baru dibuat
2. Kirim `/start` ke bot
3. Buka: `https://api.telegram.org/bot<TOKEN>/getUpdates`
4. Cari field `"chat"` → `"id"` → itu adalah chat ID kamu

Isi `TELEGRAM_CONTROL_CHAT_ID` dengan ID tersebut.

### 3.4 Grup sinyal
1. Masuk ke grup sinyal lewat akun Telegram pribadi (bukan bot)
2. Cari `SIGNAL_CHANNEL_ID` (ID negatif untuk grup):
   - Forward pesan dari grup ke [@userinfobot](https://t.me/userinfobot)
   - Atau gunakan Telethon: `await client.get_entity('nama_grup')`
3. Jika grup pakai **Topics/Forum**, catat juga `SIGNAL_THREAD_ID` (ID thread [FUTURES] - Signals)

---

## 4. Konfigurasi `.env`

```bash
cp .env.example .env
```

Edit `.env` dengan nilai nyata:

```env
# ── Bitget Demo ──────────────────────────────────────────────────────────
BITGET_API_KEY=your_demo_api_key_here
BITGET_API_SECRET=your_demo_secret_here
BITGET_API_PASSPHRASE=your_demo_passphrase_here
BITGET_USE_SANDBOX=true          # TRUE untuk demo, false untuk live

# ── Telegram Listener (Telethon — akun pribadi) ──────────────────────────
TELEGRAM_API_ID=12345678
TELEGRAM_API_HASH=abcdef1234567890abcdef1234567890
TELEGRAM_PHONE=+628xxxxxxxxxx

# ── Telegram Control Bot ─────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN=123456789:AAxxxxxxxxxxxxxxxxxxxxxx
TELEGRAM_CONTROL_CHAT_ID=123456789  # Chat ID kamu (bukan grup sinyal)

# ── Grup sinyal ──────────────────────────────────────────────────────────
SIGNAL_CHANNEL_ID=-1001234567890
SIGNAL_THREAD_ID=456              # ID thread [FUTURES] - Signals (opsional)

# ── Mode operasi ─────────────────────────────────────────────────────────
DRY_RUN=true                      # true = tidak ada order real ke exchange
LOG_LEVEL=INFO

# ── Risk default ─────────────────────────────────────────────────────────
DEFAULT_RISK_MODE=percent         # percent atau fixed_usd
DEFAULT_RISK_PERCENT=1.0
DEFAULT_MAX_LOSS_USD=5.0
DEFAULT_CONFLICT_MODE=ask         # skip | ask | add | replace

# ── Circuit breaker ──────────────────────────────────────────────────────
CB_ERROR_THRESHOLD=3
CB_TIME_WINDOW_SECONDS=300

# ── Database ─────────────────────────────────────────────────────────────
DB_PATH=data/bot.db
```

---

## 5. Mode Operasi

Bot memiliki **tiga lapisan** kontrol yang bisa dikombinasikan:

| Mode | Setting | Efek |
|------|---------|------|
| **Dry Run** | `DRY_RUN=true` | Pipeline berjalan penuh tapi **tidak ada order** yang dikirim ke exchange. Semua dicatat di log dan database sebagai simulasi. |
| **Sandbox** | `BITGET_USE_SANDBOX=true` | Order dikirim ke **Bitget Demo**, bukan akun live. |
| **Live** | `DRY_RUN=false` + `BITGET_USE_SANDBOX=false` | Order **nyata** ke akun Bitget live. Hati-hati! |

### Urutan yang benar:
```
Phase 1: DRY_RUN=true  + SANDBOX=true   → tes logika pipeline
Phase 2: DRY_RUN=false + SANDBOX=true   → tes eksekusi order ke demo
Phase 3: DRY_RUN=false + SANDBOX=false  → live trading (setelah semua Phase 1-2 lolos)
```

---

## 6. Menjalankan Bot

### 6.1 Jalankan pertama kali
```bash
# Aktifkan venv
source .venv/bin/activate

# Jalankan (Telethon akan minta OTP saat pertama kali)
python main.py
```

Saat startup, bot akan:
1. Validasi semua env var — crash jika ada yang kosong
2. Init database SQLite
3. Start control bot (polling Telegram)
4. **Bot mulai dalam mode PAUSED** — tidak ada eksekusi sinyal sampai `/resume`

### 6.2 Kirim `/resume` ke control bot
```
/resume
```
Bot akan pindah ke HALF-OPEN state, coba koneksi test, lalu mulai aktif.

### 6.3 Jalankan sebagai background process (VPS)
```bash
# Dengan nohup
nohup python main.py > /var/log/bitget-bot/bot.log 2>&1 &

# Atau dengan systemd (lebih baik)
# Lihat docs/bitget-bot.service untuk template unit file
```

---

## 7. Urutan Pengujian Demo

Jalankan dalam urutan ini. Jangan lanjut ke fase berikutnya sampai fase sebelumnya lolos.

### Phase 1 — Unit Tests (tanpa API key)
```bash
pytest tests/ -v --ignore=tests/test_demo_live.py
```
Semua harus ✅ pass.

### Phase 2 — Demo Live Tests (butuh API key Demo)
```bash
# Set env var
export DEMO_LIVE_TESTS=true

# Jalankan semua demo tests
pytest tests/test_demo_live.py -v -m demo

# Atau satu per satu:
pytest tests/test_demo_live.py::test_demo_connection -v -m demo
pytest tests/test_demo_live.py::test_risk_percent_mode -v -m demo
pytest tests/test_demo_live.py::test_risk_fixed_usd_mode -v -m demo
pytest tests/test_demo_live.py::test_leverage_safety_sl_very_close -v -m demo
pytest tests/test_demo_live.py::test_multi_position_cross_recheck -v -m demo
pytest tests/test_demo_live.py::test_circuit_breaker_trip_on_critical_errors -v -m demo
pytest tests/test_demo_live.py::test_circuit_breaker_half_open_to_closed -v -m demo
pytest tests/test_demo_live.py::test_e2e_dry_run_sandbox -v -m demo
pytest tests/test_demo_live.py::test_invalid_pair_rejected -v -m demo
```

### Phase 3 — Skenario Manual di Demo (DRY_RUN=false, SANDBOX=true)

Edit `.env`: `DRY_RUN=false`, `BITGET_USE_SANDBOX=true`, lalu jalankan bot.

Ikuti checklist manual berikut:

**A. Sinyal Valid**
- [ ] Kirim pesan sinyal format lengkap ke grup Telegram yang dimonitor
- [ ] Cek notifikasi masuk ke control bot: ringkasan sinyal sebelum eksekusi
- [ ] Verifikasi order muncul di Bitget Demo (Paper Trading → Futures → Orders)
- [ ] Verifikasi SL ter-set otomatis setelah entry fill
- [ ] Cek database: `sqlite3 data/bot.db "SELECT * FROM trades ORDER BY id DESC LIMIT 1;"`

**B. Sinyal Ambigu**
- [ ] Kirim pesan format tidak lengkap (tanpa SL)
- [ ] Cek notifikasi: inline button muncul (Eksekusi / Abaikan / Edit dulu)
- [ ] Test tombol "Abaikan"
- [ ] Test tombol "Eksekusi" — pastikan pipeline lanjut ke eksekusi

**C. Konflik Posisi**
- [ ] Buka posisi ETH/USDT manual di Demo
- [ ] Kirim sinyal baru ETH/USDT ke grup
- [ ] Cek notifikasi konflik muncul dengan inline button
- [ ] Test tombol "Tambah" — posisi ke-2 terbuka
- [ ] Test tombol "Abaikan" — sinyal dilewati

**D. Mode Risk**
- [ ] Kirim `/setrisk 2` → verifikasi mode berubah via `/riskmode`
- [ ] Kirim sinyal → verifikasi risk_amount = 2% dari balance
- [ ] Kirim `/setmaxloss 10` → verifikasi mode Fixed USD aktif
- [ ] Kirim sinyal → verifikasi risk_amount = $10 (konstan)

**E. Leverage Safety**
- [ ] Kirim sinyal dengan SL sangat dekat (SL 0.1% dari entry)
- [ ] Cek notifikasi: apakah leverage diturunkan otomatis?
- [ ] Cek log: `grep "leverage_adjusted" logs/bitget_bot.log`

**F. Multi-Posisi Cross**
- [ ] Buka 3+ posisi di pair berbeda
- [ ] Kirim `/positions` — semua posisi terdaftar
- [ ] Verifikasi recheck alert muncul jika ada posisi yang mendekati liquidation bersama

**G. Circuit Breaker**
- [ ] Matikan koneksi internet sementara (atau set API key salah)
- [ ] Cek notifikasi: circuit breaker trip setelah 3 error
- [ ] Kembalikan koneksi/API key → kirim `/resume`
- [ ] Cek `/status` — semua komponen kembali CLOSED

**H. Pause & Resume**
- [ ] Kirim `/pause`
- [ ] Kirim sinyal baru → tidak dieksekusi
- [ ] Verifikasi monitoring posisi existing tetap berjalan (cek log)
- [ ] Kirim `/resume` → eksekusi aktif kembali

**I. Emergency**
- [ ] Buka 2-3 posisi di Demo
- [ ] Kirim `/closeall` → semua posisi ter-close
- [ ] Verifikasi di Bitget Demo semua posisi sudah tutup

---

## 8. Monitoring & Log

### 8.1 Lokasi log
```
logs/
├── bitget_bot.log          # Log utama (INFO+)
├── bitget_bot.log.1        # Rotasi
└── bitget_bot_error.log    # ERROR+ saja
```

### 8.2 Perintah monitoring berguna

```bash
# Tail log realtime
tail -f logs/bitget_bot.log

# Filter error saja
grep -E "ERROR|CRITICAL" logs/bitget_bot.log | tail -20

# Cek circuit breaker state
grep "circuit_breaker" logs/bitget_bot.log | tail -10

# Cek sinyal yang masuk hari ini
grep "$(date +%Y-%m-%d)" logs/bitget_bot.log | grep "pipeline"

# Query database langsung
sqlite3 data/bot.db "SELECT pair, direction, status, pnl FROM trades ORDER BY id DESC LIMIT 10;"
sqlite3 data/bot.db "SELECT component, state, consecutive_error_count FROM circuit_breaker_state;"
sqlite3 data/bot.db "SELECT key, value FROM settings;"
```

### 8.3 Telegram commands untuk monitoring

```
/status      → health tiap komponen (circuit breaker state, WS, listener)
/dashboard   → ringkasan: posisi open, balance, P&L hari ini
/positions   → detail tiap posisi open
/pending     → limit order yang belum fill
/settings    → semua setting aktif
```

---

## 9. Command Telegram Referensi Cepat

### Risk Management
```
/setrisk 1          → max loss = 1% dari total balance (mode Percent)
/setmaxloss 5       → max loss = $5 per trade (mode Fixed USD)
/riskmode           → tampilkan mode aktif
```

### Leverage
```
/setleverage ETH 20 → cap leverage ETH ke max 20x
/leverage ETH       → cek max leverage + liquidation buffer
```

### Posisi
```
/positions          → list semua posisi open
/settp ETH 3200     → set Take Profit ETH di $3200
/setsl ETH 2900     → geser Stop Loss ETH ke $2900
/close ETH          → close posisi ETH (market)
/closeall           → emergency: close semua posisi
/pending            → list limit order yang belum fill
/cancel ETH         → cancel limit order ETH
```

### Kontrol
```
/pause              → stop eksekusi sinyal baru (monitoring posisi tetap jalan)
/resume             → aktifkan kembali / reset circuit breaker
/status             → health check semua komponen
/conflictmode ask   → tanya konfirmasi jika ada konflik posisi (default)
/conflictmode skip  → abaikan sinyal jika pair sudah ada posisi
/conflictmode add   → selalu tambah posisi tanpa konfirmasi
/conflictmode replace → replace posisi lama tanpa konfirmasi
```

### Info
```
/dashboard          → ringkasan balance, posisi, P&L
/history 10         → 10 trade terakhir
/settings           → semua config aktif
```

---

## 10. Checklist Go-Live (API Key Real)

Centang SEMUA sebelum mengganti ke API key live:

### ✅ Testing selesai
- [ ] `pytest tests/ -v` → semua unit test pass
- [ ] `DEMO_LIVE_TESTS=true pytest tests/test_demo_live.py -v -m demo` → semua pass
- [ ] Semua skenario manual Phase 3 lolos
- [ ] Circuit breaker trip & recovery sudah diverifikasi
- [ ] Kedua mode risk (Percent & Fixed USD) sudah diverifikasi

### ✅ Konfigurasi aman
- [ ] `.env` tidak di-commit ke Git (ada di `.gitignore`)
- [ ] Tidak ada API key hardcode di kode manapun
- [ ] `DRY_RUN=false` di-set **secara sadar**, bukan tidak sengaja
- [ ] Risk percent sudah di-set ke nilai yang tepat (`/setrisk`)
- [ ] Conflict mode sudah dipilih (`/conflictmode`)

### ✅ Infrastruktur
- [ ] Bot berjalan di VPS (bukan laptop) dengan uptime stabil
- [ ] Log rotation berjalan (cek `logs/` tidak memenuhi disk)
- [ ] Alert Telegram terkirim ke chat ID yang benar
- [ ] Backup `data/bot.db` otomatis dijadwalkan (cron/rsync)

### ✅ Operasional
- [ ] Kamu memahami cara `/pause` untuk menghentikan entry baru
- [ ] Kamu memahami cara `/closeall` untuk emergency
- [ ] Kamu memahami bahwa **mode Cross** → semua posisi berbagi pool margin
- [ ] Kamu sudah membaca peringatan di `CATATAN PENTING` di `prompt.md`

### ✅ Langkah switch ke live
```bash
# 1. Stop bot yang sedang jalan
pkill -f "python main.py"

# 2. Edit .env
BITGET_API_KEY=live_key_here
BITGET_API_SECRET=live_secret_here
BITGET_API_PASSPHRASE=live_passphrase_here
BITGET_USE_SANDBOX=false
DRY_RUN=false

# 3. Jalankan ulang
python main.py

# 4. Verifikasi startup log:
# "SANDBOX     : False"
# "DRY_RUN     : False"

# 5. Kirim /resume untuk mulai eksekusi
```

---

## 11. Troubleshooting

### Bot tidak start: `ValueError: [CONFIG] Environment variable 'X' WAJIB`
→ Isi env var yang disebutkan di `.env`. Cek `.env.example` untuk daftar lengkap.

### Telethon: `SessionPasswordNeededError`
→ Akun Telegram kamu mengaktifkan 2FA. Isi `TELEGRAM_2FA_PASSWORD` di `.env`.

### Telethon: gagal join session setelah restart
→ Session file tersimpan di `data/telethon.session`. Jangan hapus file ini kecuali mau login ulang.

### `AuthenticationError` dari Bitget
→ API key salah, passphrase salah, atau IP tidak di-whitelist. Cek di Bitget API Management.

### Order ditolak: `invalid leverage`
→ Bitget membatasi leverage untuk pair tertentu berdasarkan tier margin. Bot akan fetch ulang max leverage secara otomatis — pastikan `set_leverage` dipanggil dengan nilai valid.

### Circuit breaker selalu OPEN
→ Cek `/status` untuk komponen mana yang trip. Baca log untuk error spesifik. Setelah perbaikan → `/resume`.

### WebSocket disconnect terus-menerus
→ Bisa karena rate limit atau koneksi tidak stabil. Bot punya reconnect otomatis. Jika masalah persisten, cek `CB_ERROR_THRESHOLD` di `.env`.

### Bot tidak merespons sinyal dari grup
→ Cek `SIGNAL_CHANNEL_ID` dan `SIGNAL_THREAD_ID`. Pastikan format angka benar (ID grup biasanya negatif, mis. `-1001234567890`).

### Posisi tidak masuk database setelah eksekusi
→ Cek log untuk `[executor]`. Mungkin order berhasil di exchange tapi ada exception saat insert ke DB. Cek `data/bot.db` manual dengan sqlite3.

---

## Catatan Versi

| Step | Tanggal | Perubahan |
|------|---------|-----------|
| Step 19 | 2026-06 | Integrasi penuh pipeline end-to-end |
| Step 20 | 2026-06 | Demo live test suite + dokumentasi final |
