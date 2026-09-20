import asyncio
import logging
from telegram.ext import ApplicationBuilder, MessageHandler, filters
from ..config import settings
from .handlers import handle_photo, handle_text, handle_edited, error_handler, worker

log = logging.getLogger(__name__)

async def start_bot():
    if not settings.bot_token or settings.bot_token == "your_bot_token_here":
        log.warning("BOT_TOKEN пустой/заглушка - бот не запускается (демо-режим)")
        for _ in range(settings.worker_concurrency):
            asyncio.create_task(worker())
        return None
    # запускаем worker'ы всегда
    for _ in range(settings.worker_concurrency):
        asyncio.create_task(worker())
    # health-check вариант В: 5 мин в окно, 20 мин вне окна
    try:
        from ..services.health import health_check_loop
        asyncio.create_task(health_check_loop())
        log.info("health-check loop запущен")
    except Exception as e:
        log.debug(f"health-check not started: {e}")
    try:
        app = ApplicationBuilder().token(settings.bot_token).build()
        app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.PHOTO, handle_text))
        # редактирование — для перепроверки через час ТЗ 9
        app.add_handler(MessageHandler(filters.UpdateType.EDITED_MESSAGE, handle_edited))
        app.add_handler(MessageHandler(filters.UpdateType.EDITED_CHANNEL_POST, handle_edited))
        app.add_error_handler(error_handler)
        await app.initialize()
        await app.start()
        # высокая активность ТЗ 9: drop_pending_updates=False чтобы не потерять события при рестарте + getUpdates с retry
        # бот мог временно не иметь доступа — polling сам ретрает с backoff, error_handler логирует
        await app.updater.start_polling(drop_pending_updates=False, allowed_updates=["message","edited_message","channel_post","edited_channel_post"])
        log.info("Telegram bot polling started for %s (workers=%s, queue=%s)", settings.bot_token[:10]+"...", settings.worker_concurrency, settings.queue_maxsize)
        return app
    except Exception as e:
        log.warning(f"Telegram bot не запустился, переход в демо-режим (веб+очередь работают): {e}")
        return None

async def stop_bot(app):
    if app:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
