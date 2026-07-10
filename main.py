"""
Bitget Signal Bot — Entry Point (Step 19: fully wired)
=======================================================
Startup:
1. Load & validasi config
2. Setup logging
3. Init database
4. Build control bot → inject Bot instance ke notifier
5. Wire inline handlers → pipeline
6. Start Telethon listener dengan on_message → pipeline
7. Start WebSocket monitor
8. Run asyncio.gather(listener, ws_monitor, control_bot)
"""

import asyncio
import sys


def _bootstrap() -> None:
    try:
        from config.settings import settings  # noqa: F401
    except ValueError as exc:
        print(f"\n{'='*60}", file=sys.stderr)
        print("STARTUP GAGAL — Konfigurasi tidak lengkap:", file=sys.stderr)
        print(str(exc), file=sys.stderr)
        print(f"{'='*60}\n", file=sys.stderr)
        sys.exit(1)

    from core.logging_setup import setup_logging
    setup_logging(
        log_level=settings.LOG_LEVEL,
        log_dir=settings.LOG_DIR,
        max_bytes=settings.LOG_MAX_BYTES,
        backup_count=settings.LOG_BACKUP_COUNT,
    )

    from core.logging_setup import get_logger
    logger = get_logger(__name__)
    logger.info("=" * 60)
    logger.info("Bitget Signal Bot — Starting up (Step 19)")
    logger.info("DRY_RUN     : %s", settings.DRY_RUN)
    logger.info("SANDBOX     : %s", settings.BITGET_USE_SANDBOX)
    logger.info("RISK MODE   : %s", settings.DEFAULT_RISK_MODE)
    logger.info("RISK VALUE  : %s%%  |  MAX LOSS USD: $%s",
                settings.DEFAULT_RISK_PERCENT, settings.DEFAULT_MAX_LOSS_USD)
    logger.info("CONFLICT    : %s", settings.DEFAULT_CONFLICT_MODE)
    logger.info("TIMEZONE    : %s (display)", settings.DISPLAY_TIMEZONE)
    logger.info("DB PATH     : %s", settings.DB_PATH)
    logger.info("=" * 60)

    if settings.DRY_RUN:
        logger.warning("⚠️  DRY RUN AKTIF — tidak ada order real yang akan dikirim ke exchange")
    if settings.BITGET_USE_SANDBOX:
        logger.warning("⚠️  SANDBOX MODE — terhubung ke Bitget Demo/Testnet")


async def main() -> None:
    from core.logging_setup import get_logger
    logger = get_logger(__name__)

    from config.settings import settings
    from db.database import init_db
    init_db()
    logger.info("Database initialized.")

    tasks = []

    # ── Control bot + notifier init ──────────────────────────────────────
    from bot.control_bot.bot import build_application
    from notifications.notifier import init_notifier

    app = build_application()
    init_notifier(app.bot, settings.TELEGRAM_CONTROL_CHAT_ID)
    logger.info("Notifier initialized.")

    # ── Pipeline + wiring ────────────────────────────────────────────────
    from bot.pipeline.signal_pipeline import get_pipeline
    from bot.pipeline.wiring import wire_inline_handlers

    pipeline = get_pipeline()
    wire_inline_handlers(pipeline)
    logger.info("Pipeline wired.")

    # ── Telethon listener ────────────────────────────────────────────────
    # Butuh TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_PHONE di .env.
    # Saat pertama kali jalan, Telethon akan minta kode OTP di terminal.
    from bot.telegram_listener import start_listener
    listener = await start_listener(on_message=pipeline.process_raw_event)
    tasks.append(asyncio.create_task(listener.run_until_disconnected()))
    logger.info("Telethon listener started.")

    # ── WebSocket monitor ────────────────────────────────────────────────
    # Butuh BITGET_API_KEY, BITGET_API_SECRET, BITGET_PASSPHRASE valid di .env.
    from exchange.bitget.ws_client import get_ws_client
    ws = get_ws_client()
    await ws.start()
    logger.info("WebSocket monitor started.")

    # ── Control bot task ─────────────────────────────────────────────────
    async def _run_control_bot() -> None:
        async with app:
            await app.start()
            await app.updater.start_polling(
                allowed_updates=["message", "callback_query"],
                drop_pending_updates=True,
            )
            logger.info("Control bot polling aktif. Bot dimulai dalam mode PAUSED — kirim /resume.")
            try:
                await asyncio.get_event_loop().create_future()
            except asyncio.CancelledError:
                pass
            finally:
                await app.updater.stop()
                await app.stop()

    tasks.append(asyncio.create_task(_run_control_bot()))

    if tasks:
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            pass
        finally:
            # ── Shutdown bersih: tutup listener Telethon & WS monitor ────
            try:
                await listener.stop()
                logger.info("Telethon listener stopped.")
            except Exception as exc:
                logger.warning("Gagal stop Telethon listener: %s", exc)
            try:
                await ws.stop()
                logger.info("WebSocket monitor stopped.")
            except Exception as exc:
                logger.warning("Gagal stop WebSocket monitor: %s", exc)


if __name__ == "__main__":
    _bootstrap()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        import logging
        logging.getLogger(__name__).info("Bot dihentikan manual (KeyboardInterrupt)")
        sys.exit(0)