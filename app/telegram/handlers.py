import asyncio
import logging
from datetime import datetime
from sqlalchemy import select
from ..database import SessionLocal
from ..services.processor import process_message
from ..config import settings

log = logging.getLogger(__name__)

# Очередь для гарантии обработки при пиках (ТЗ 11)
queue: asyncio.Queue = asyncio.Queue(maxsize=settings.queue_maxsize)

async def worker():
    while True:
        payload = await queue.get()
        try:
            async with SessionLocal() as db:
                await process_message(db, payload)
        except Exception as e:
            log.exception(f"worker error {e} payload={payload}")
        finally:
            queue.task_done()

async def enqueue_message(payload: dict):
    # не теряем события, ставим в очередь с гарантией
    # высокая активность ТЗ 9: мониторинг очереди, при 80%+ логируем алерт
    if queue.qsize() > settings.queue_maxsize * 0.8:
        log.warning(f"высокая активность: очередь {queue.qsize()}/{settings.queue_maxsize} — много нерелевантных фото, обработка может задерживаться но не теряется")
    # нерелевантные фото (нет человека и нет подписи с сотрудником) все равно ставим в очередь, но помечаем low priority — в MVP просто очередь, в проде можно отдельный low-prio воркер
    await queue.put(payload)

async def handle_edited(update, context):
    """Сообщение отредактировано после обработки — обновляем caption и логируем.
    Фактическая перепроверка — через schedule_recheck в processor, но тут сразу фиксируем факт."""
    msg = update.edited_message or update.edited_channel_post
    if not msg:
        return
    chat = msg.chat
    new_caption = msg.caption or msg.text or ""
    # обновляем MessageLog caption для последующей фаззи-проверки schedule_recheck
    try:
        from ..database import SessionLocal
        from ..models import MessageLog, AuditLog
        from sqlalchemy import select
        async with SessionLocal() as db:
            res = await db.execute(select(MessageLog).where(MessageLog.tg_chat_id==chat.id, MessageLog.tg_message_id==msg.message_id))
            ml = res.scalar_one_or_none()
            if ml:
                old = ml.caption
                ml.caption = new_caption
                ml.raw_json = ml.raw_json + f" | edited: {new_caption}"
                db.add(AuditLog(action="edited", entity="MessageLog", entity_id=ml.id, detail=f"'{old}' -> '{new_caption}'"))
                await db.commit()
                log.info(f"edited {chat.id}:{msg.message_id} '{old}' -> '{new_caption}'")
    except Exception as e:
        log.exception(f"handle_edited error {e}")

async def error_handler(update, context):
    """Потеря доступа бота / лимиты Telegram — ТЗ 9"""
    err = str(context.error) if context.error else "unknown"
    log.warning(f"Telegram error: {err} update={update}")
    # бот кикнут / нет прав
    if "forbidden" in err.lower() or "kicked" in err.lower() or "not enough rights" in err.lower():
        log.error(f"Бот потерял доступ к чату — проверьте админку группы, бот должен быть админом. Ошибка: {err}")
        # алерт
        try:
            from ..services.processor import send_alert
            await send_alert(f"⚠️ Бот потерял доступ: {err}")
        except: pass
    elif "timeout" in err.lower() or "timed out" in err.lower() or "network" in err.lower():
        log.warning(f"Telegram ограничил/таймаут — retry через backoff: {err}")
        # PTB сам ретрает, но логируем
    elif "too many requests" in err.lower() or "retry after" in err.lower():
        log.warning(f"Telegram rate limit: {err} — очередь сохраняет события")
    # не пробрасываем — чтобы polling не упал

# Telegram handlers (python-telegram-bot v20)
async def handle_photo(update, context):
    msg = update.message or update.channel_post
    if not msg:
        return
    chat = msg.chat
    has_photo = bool(msg.photo)
    caption = msg.caption or ""
    # user может быть None в каналах
    from_user = msg.from_user
    # пытаемся скачать байты фото для нейронки/OCR (если VISION_ENABLED)
    image_bytes = None
    if has_photo and msg.photo and settings.vision_enabled:
        try:
            file = await context.bot.get_file(msg.photo[-1].file_id)
            import io
            buf = io.BytesIO()
            await file.download_to_memory(buf)
            image_bytes = buf.getvalue()
            log.info(f"фото скачано {len(image_bytes)} байт file_id={msg.photo[-1].file_id} для vision")
        except Exception as e:
            log.warning(f"download photo failed {e} — будет fallback по подписи")
    payload = {
        "tg_message_id": msg.message_id,
        "tg_chat_id": chat.id,
        "date_time": msg.date,  # already datetime aware UTC
        "has_photo": has_photo,
        "caption": caption,
        "from_user_id": from_user.id if from_user else None,
        "from_username": from_user.username if from_user else None,
        "file_id": msg.photo[-1].file_id if has_photo and msg.photo else None,
        "image_bytes": image_bytes,
    }
    await enqueue_message(payload)

async def handle_text(update, context):
    # Текст без фото - тоже логируем как rejected в обработчике
    msg = update.message or update.channel_post
    if not msg:
        return
    chat = msg.chat
    from_user = msg.from_user
    payload = {
        "tg_message_id": msg.message_id,
        "tg_chat_id": chat.id,
        "date_time": msg.date,
        "has_photo": False,
        "caption": msg.text or "",
        "from_user_id": from_user.id if from_user else None,
        "from_username": from_user.username if from_user else None,
    }
    await enqueue_message(payload)
