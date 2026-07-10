# Bitget Signal Bot

Bot auto-trading Bitget Futures berbasis sinyal dari grup Telegram privat.

> ⚠️ **Peringatan**: Trading futures dengan leverage berisiko tinggi.
> Selalu uji di akun demo/testnet sebelum menyambungkan API key live.
> Bot ini bukan nasihat finansial.

---

## Arsitektur Singkat

```
Telegram Grup (sinyal)
        │
        ▼
[Telethon Listener]          ← akun pribadi user (baca pasif)
        │
        ▼
[Signal Parser]              ← extract pair, entry, SL, direction
        │
        ▼
[Risk Engine]                ← hitung position_size & margin_needed
        │
        ▼
[Leverage Safety Engine]     ← validasi SL vs proyeksi liquidation (cross mode)
        │
        ▼
[Position Checker]           ← cek konflik posisi/order existing
        │
        ▼
[Bitget Executor]            ← eksekusi order via ccxt REST
        │
        ├── [ccxt.pro WS]    ← monitor posisi & order realtime
        └── [Circuit Breaker]← trip jika ada critical error beruntun

[Telegram Control Bot]       ← command /dashboard /setrisk /close dll.
```

## Struktur Folder

```
bitget-signal-bot/
├── main.py                     # Entry point
├── .env.example                # Template environment variables
├── .gitignore                  # File/folder yang tidak di-commit ke git (.env, session, db, log)
├── requirements.txt
├── config/                     # Config loader & validasi env vars
├── core/                       # Konstanta global
├── bot/
│   ├── telegram_listener/      # Telethon: baca sinyal dari grup
│   ├── control_bot/            # python-telegram-bot: command handler
│   ├── parser/                 # Signal parser & confidence scoring
│   ├── executor/               # Eksekusi order di Bitget
│   ├── risk_engine/            # Kalkulasi risk & margin
│   ├── leverage_engine/        # Safety check liquidation (cross)
│   ├── position_checker/       # Cek konflik posisi/order
│   └── circuit_breaker/        # State machine circuit breaker
├── exchange/
│   └── bitget/                 # ccxt REST + ccxt.pro WebSocket connector
├── db/                         # SQLite schema & CRUD
├── notifications/               # Modul kirim notifikasi Telegram
└── tests/                      # Unit tests
```

## Setup

Panduan lengkap: **[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)**

```bash
# 1. Clone & masuk folder
git clone <repo> bitget-signal-bot
cd bitget-signal-bot

# 2. Buat virtual environment
python3.11 -m venv .venv
source .venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Konfigurasi environment
cp .env.example .env
# Edit .env — isi SEMUA field wajib (lihat config/settings.py atau
# docs/DEPLOYMENT.md untuk penjelasan tiap field). Bot akan gagal start
# kalau ada field wajib yang kosong.

# 5. Jalankan dalam DRY_RUN dulu
python main.py
```

> ⚠️ **Percobaan pertama kali**: `main.py` langsung menyalakan Telethon listener
> dan WebSocket monitor Bitget saat start (bukan opsional lagi). Pastikan
> `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` / `TELEGRAM_PHONE` dan
> `BITGET_API_KEY` / `BITGET_API_SECRET` / `BITGET_PASSPHRASE` di `.env`
> sudah diisi valid sebelum menjalankan. Saat pertama kali run, Telethon akan
> minta **kode OTP** yang dikirim ke akun Telegram pribadimu — masukkan
> langsung di terminal (hanya terjadi sekali, session tersimpan setelahnya).
>
> Setelah bot jalan, kirim `/resume` ke control bot untuk mengaktifkan eksekusi sinyal.

## Aturan Penting

- **DRY_RUN=true** selalu aktif sampai semua pengujian demo selesai
- Bot mulai dalam mode **paused** — harus eksplisit `/resume` dulu
- Gunakan **Bitget Demo/Paper Trading API** (`BITGET_USE_SANDBOX=true`) sampai checklist go-live di `docs/DEPLOYMENT.md` selesai
- Semua waktu backend UTC, tampil ke user dalam Asia/Jakarta
- Mode Cross + leverage tinggi → recheck liquidation gabungan berjalan otomatis tiap ada posisi baru/tutup

## Menjalankan Test

```bash
# Unit tests (tanpa API key)
pytest tests/ -v --ignore=tests/test_demo_live.py
# Status: 365 passed, 0 failed

# Demo live tests (butuh API key Bitget Demo)
DEMO_LIVE_TESTS=true pytest tests/test_demo_live.py -v -m demo
```

## Progress Build (20 Step)

- [x] Step 1 — Setup project & struktur folder
- [x] Step 2 — Config & environment layer
- [x] Step 3 — Telethon listener
- [x] Step 4 — Signal parser (field dasar)
- [x] Step 5 — Signal parser (ambiguitas & non-entry)
- [x] Step 6 — Database layer
- [x] Step 7 — Bitget connector REST
- [x] Step 8 — Bitget connector WebSocket
- [x] Step 9 — Risk & margin engine
- [x] Step 10 — Leverage safety engine (cross-aware)
- [x] Step 11 — Position checker
- [x] Step 12 — Executor: open position
- [x] Step 13 — Executor: SL, close & manajemen order
- [x] Step 14 — Circuit breaker
- [x] Step 15 — Control bot: info commands
- [x] Step 16 — Control bot: risk & leverage commands
- [x] Step 17 — Control bot: position management commands
- [x] Step 18 — Control bot: inline buttons & konfirmasi
- [x] Step 19 — Integrasi penuh & dry-run end-to-end
- [x] Step 20 — Testing live di Bitget Demo & dokumentasi final ✅

**Build selesai.** Ikuti checklist di `docs/DEPLOYMENT.md` → bagian "Checklist Go-Live" sebelum switch ke API key live.
