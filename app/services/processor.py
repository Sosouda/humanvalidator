from datetime import datetime, date
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from ..models import TelegramGroup, Employee, Facility, ShiftMark, MessageLog, AuditLog, ProcessingStatus
from .identifier import identify_employee, extract_multiple_employees, fuzzy_ratio, find_fuzzy
from .shift_resolver import resolve_shift_date
from .vision import detect_person, extract_datetime_neural
from ..config import settings
import json
import asyncio
import logging
log = logging.getLogger(__name__)
# храним задачи чтобы не GC
_recheck_tasks = set()

async def send_alert(text: str):
    """Шлет алерт в admin_chat_id если задан, иначе лог."""
    if not settings.admin_chat_id:
        log.warning(f"ALERT (no admin_chat): {text}")
        return
    try:
        from telegram import Bot
        bot = Bot(token=settings.bot_token)
        await bot.send_message(chat_id=settings.admin_chat_id, text=text)
    except Exception as e:
        log.warning(f"alert send failed: {e} text={text}")

async def schedule_recheck(tg_chat_id: int, tg_msg_id: int, employee_name: str, shift_date: date, original_caption: str, delay: int = None):
    """
    Через час после 1 перепроверяет сообщение: если отредактировано/удалено — алерт и запись в лог.
    Сравнение не буква в букву — фаззи ratio >= recheck_fuzzy_threshold.
    """
    delay = delay if delay is not None else settings.recheck_delay_seconds
    log.warning(f"recheck scheduled {tg_chat_id}:{tg_msg_id} in {delay}s original='{original_caption}'")
    await asyncio.sleep(delay)
    log.warning(f"recheck firing {tg_chat_id}:{tg_msg_id}")
    from ..database import SessionLocal
    try:
        async with SessionLocal() as db:
            # найдем лог
            res = await db.execute(select(MessageLog).where(MessageLog.tg_chat_id==tg_chat_id, MessageLog.tg_message_id==tg_msg_id))
            ml = res.scalar_one_or_none()
            log.warning(f"recheck fetched ml={ml.id if ml else None} status={ml.status if ml else None} caption={ml.caption if ml else None}")
            if not ml or ml.status != ProcessingStatus.accepted.value:
                log.warning(f"recheck skip not accepted {ml}")
                return  # уже не актуально
            # попытка получить свежий текст через Bot API (forward как проверка существования)
            # Для edited — у нас есть handler handle_edited который уже обновит ml.caption, поэтому сверяем с ним
            # Для deleted — пробуем forward_message в тот же чат как тест
            new_caption = ml.caption  # после возможного edit handler
            # проверка редактирования — фаззи сравнение (сначала, без сетевых запросов)
            log.warning(f"recheck compare original='{original_caption}' new='{new_caption}' ratio={fuzzy_ratio(original_caption or '', new_caption or ''):.2f} threshold={settings.recheck_fuzzy_threshold}")
            ratio = fuzzy_ratio(original_caption or "", new_caption or "")
            if ratio < settings.recheck_fuzzy_threshold:
                facility_name = ""
                if ml.facility_id:
                    fac = await db.get(Facility, ml.facility_id)
                    facility_name = fac.name if fac else ""
                alert = f"⚠️ Изменено сообщение-основание: объект {facility_name}, сотрудник {employee_name}, смена {shift_date}, ratio={ratio:.2f} '{original_caption}' -> '{new_caption}'"
                log.warning(alert)
                await send_alert(alert)
                ml.reason += f" | перепроверка через час: подпись изменена ratio={ratio:.2f} -> ручная"
                ml.status = ProcessingStatus.manual.value
                db.add(AuditLog(action="recheck_edited", entity="MessageLog", entity_id=ml.id, detail=alert))
                await db.commit()
                return
            # если подпись не менялась — проверяем удаление через API (только для реальных чатов, с таймаутом)
            # используем forward как проверку, но сразу удаляем форвард чтобы не спамить рабочий чат
            if settings.bot_token and settings.bot_token != "your_bot_token_here" and not str(tg_chat_id).startswith("-100999"):
                try:
                    from telegram import Bot
                    bot = Bot(token=settings.bot_token)
                    try:
                        fwd = await asyncio.wait_for(bot.forward_message(chat_id=tg_chat_id, from_chat_id=tg_chat_id, message_id=tg_msg_id, disable_notification=True), timeout=5)
                        # сразу удаляем служебный форвард — он нужен только для проверки, не должен оставаться в чате
                        try:
                            await bot.delete_message(chat_id=tg_chat_id, message_id=fwd.message_id)
                        except Exception:
                            pass
                    except asyncio.TimeoutError:
                        log.debug("recheck forward timeout, skip")
                    except Exception as e:
                        err = str(e).lower()
                        if "chat not found" in err or "chat_id is empty" in err:
                            log.debug(f"recheck chat not found, skip delete check: {e}")
                        elif "not found" in err or "deleted" in err or "message to forward not found" in err or "message_id_invalid" in err:
                            facility_name = ""
                            if ml.facility_id:
                                fac = await db.get(Facility, ml.facility_id)
                                facility_name = fac.name if fac else str(ml.facility_id)
                            alert = f"⚠️ Удалено сообщение-основание: объект {facility_name}, сотрудник {employee_name}, смена {shift_date}, msg {tg_chat_id}:{tg_msg_id} — 1 в таблице больше не подтверждена"
                            log.warning(alert)
                            await send_alert(alert)
                            ml.status = ProcessingStatus.manual.value
                            ml.reason += f" | перепроверка через час: сообщение удалено -> требует ручной проверки"
                            db.add(AuditLog(action="recheck_deleted", entity="MessageLog", entity_id=ml.id, detail=alert))
                            await db.commit()
                            return
                        else:
                            log.debug(f"recheck forward other error: {e}")
                except Exception as e:
                    log.debug(f"recheck forward check failed: {e}")
            # если дошли сюда — все ок
            log.warning(f"recheck ok {tg_chat_id}:{tg_msg_id} ratio={ratio:.2f}")
            db.add(AuditLog(action="recheck_ok", entity="MessageLog", entity_id=ml.id, detail=f"ratio {ratio:.2f}"))
            await db.commit()
            return
    except Exception as e:
        log.exception(f"schedule_recheck error {e}")

async def process_message(db: AsyncSession, payload: dict) -> MessageLog:
    """
    payload: dict с ключами tg_message_id, tg_chat_id, date_time, has_photo, caption, from_user_id, from_username, file_id
    Реализует бизнес-логику ТЗ 4,6,7,8,9
    """
    tg_msg_id = payload["tg_message_id"]
    tg_chat_id = payload["tg_chat_id"]
    has_photo = payload.get("has_photo", False)
    caption = payload.get("caption") or ""
    from_user_id = payload.get("from_user_id")
    from_username = payload.get("from_username")
    dt = payload["date_time"]
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt)

    # 1. идемпотентность по tg_chat_id+tg_message_id
    existing = await db.execute(select(MessageLog).where(MessageLog.tg_chat_id==tg_chat_id, MessageLog.tg_message_id==tg_msg_id))
    ml = existing.scalar_one_or_none()
    if ml:
        # уже обрабатывали - логируем дубль но не создаем повторно отметку
        return ml

    # 2. найти группу
    grp_res = await db.execute(select(TelegramGroup).where(TelegramGroup.tg_id==tg_chat_id))
    group = grp_res.scalar_one_or_none()
    facility = None
    if group and group.facility_id:
        fac_res = await db.execute(select(Facility).where(Facility.id==group.facility_id))
        facility = fac_res.scalar_one_or_none()

    # §7: супергруппы/каналы имеют t.me/c/ ссылку, обычные группы — нет (только tg://)
    # определяем тип группы если известен
    group_type = getattr(group, "group_type", "supergroup") if group else "supergroup"
    if str(tg_chat_id).startswith("-100"):
        base_link = f"https://t.me/c/{str(tg_chat_id).replace('-100','')}/{tg_msg_id}"
        if group_type not in ("supergroup","channel"):
            base_link += " (требуется супергруппа/канал для ссылки — сейчас t.me/c сгенерирована условно)"
    elif str(tg_chat_id).startswith("-"):
        base_link = f"tg://private/{str(tg_chat_id).lstrip('-')}/{tg_msg_id} (обычная группа — t.me/c недоступна, нужна супергруппа §7)"
    else:
        base_link = f"tg://{tg_chat_id}/{tg_msg_id}"

    ml = MessageLog(
        tg_message_id=tg_msg_id,
        tg_chat_id=tg_chat_id,
        group_id=group.id if group else None,
        date_time=dt,
        has_photo=has_photo,
        caption=caption,
        from_user_id=from_user_id,
        from_username=from_username,
        facility_id=facility.id if facility else None,
        base_link=base_link,
        raw_json=json.dumps({k: v for k, v in payload.items() if k != "image_bytes"}, ensure_ascii=False, default=str),
    )

    # ТЗ 4.1: основной триггер - фото
    if not has_photo:
        ml.status = ProcessingStatus.rejected.value
        ml.reason = "нет фотографии -> отклонено (ТЗ 4.1, только фото-триггер)"
        db.add(ml)
        await db.commit()
        return ml

    # §2: пайплайн: нейронка человека -> если нашла, нейронка даты/времени с фото
    image_bytes = payload.get("image_bytes")  # может прийти из TG file download
    person_detected, vision_reason = await detect_person(image_bytes, has_photo)
    ml.vision_person_detected = person_detected
    ml.vision_reason = vision_reason

    # если человека нет — отклоняем (не на проверку), как попросили
    if person_detected is False:
        ml.status = ProcessingStatus.rejected.value
        ml.reason = f"нет человека на фото -> отклонено | {vision_reason} | caption='{caption}'"
        ml.photo_ocr_text = ""
        ml.photo_ocr_datetime = None
        dt_for_shift = dt
        db.add(ml)
        await db.commit()
        return ml

    # если человек найден (True) — вызываем нейронку даты/времени
    if person_detected is True:
        ocr_text, ocr_dt, ocr_reason = await extract_datetime_neural(image_bytes, caption)
        ml.photo_ocr_text = ocr_text[:500] if ocr_text else ""
        ml.photo_ocr_datetime = ocr_dt
        # комбинируем reason
        ml.vision_reason = f"{vision_reason} | {ocr_reason}"
        if ocr_dt:
            dt_for_shift = ocr_dt
        else:
            dt_for_shift = dt
            # строгий режим: если дата не считана — на ручную (требует точного времени с фото)
            if settings.vision_strict:
                ml.status = ProcessingStatus.manual.value
                ml.reason = f"нейронка: человек найден, но дата/время с фото не считаны -> ручная проверка | {ocr_reason} | caption='{caption}'"
                ml.shift_date = None
                db.add(ml)
                await db.commit()
                return ml
    else:
        # vision отключен (None) — пропускаем обе нейронки, используем время сообщения
        ml.photo_ocr_text = ""
        ml.photo_ocr_datetime = None
        dt_for_shift = dt
        # vision_reason уже "vision отключен"

    # ТЗ 9: высокая активность - но для MVP просто обработать

    # 3. определить сотрудника(s)
    all_emps_res = await db.execute(select(Employee).where(Employee.is_active==True))
    all_emps = all_emps_res.scalars().all()
    multi = extract_multiple_employees(caption, all_emps)

    # Случай: в одном сообщении несколько сотрудников (например 2 человека на 1 телефон)
    # ТЗ 9: по умолчанию ручная, НО если объект закреплен за фиксированным составом и все найденные входят в allowed — обрабатываем каждого
    if len(multi) > 1:
        allowed_set = set()
        if group and group.allowed_employee_ids:
            try:
                allowed_set = set(json.loads(group.allowed_employee_ids))
            except:
                allowed_set = set()
        multi_ids = set(e.id for e in multi)
        if allowed_set and multi_ids.issubset(allowed_set):
            # обрабатываем каждого как отдельную отметку с одной фотографии
            # dt_for_shift уже определен выше (после vision)
            shift_date, within_window, window_reason = resolve_shift_date(dt_for_shift, facility, settings.timezone)
            if not within_window:
                ml.status = ProcessingStatus.manual.value
                ml.reason = f"несколько сотрудников ({', '.join([e.full_name for e in multi])}) вне окна {window_reason} -> ручная"
                ml.shift_date = shift_date
                db.add(ml)
                await db.commit()
                return ml
            # проверяем объект флаг
            if facility and not getattr(facility, "auto_mark_allowed", True):
                ml.status = ProcessingStatus.manual.value
                ml.reason = f"объект {facility.name} без автоотметки, несколько сотрудников -> ручная"
                ml.shift_date = shift_date
                db.add(ml)
                await db.commit()
                return ml
            created = []
            dups = []
            for emp_m in multi:
                dup_q = await db.execute(select(ShiftMark).where(ShiftMark.employee_id==emp_m.id, ShiftMark.shift_date==shift_date, ShiftMark.facility_id==(facility.id if facility else None)))
                ex = dup_q.scalar_one_or_none()
                if ex:
                    dups.append(emp_m.full_name)
                else:
                    mark = ShiftMark(employee_id=emp_m.id, shift_date=shift_date, facility_id=facility.id if facility else None, value=1, message_id=f"{tg_chat_id}:{tg_msg_id}:{emp_m.id}", auto_created=True)
                    db.add(mark)
                    created.append(emp_m.full_name)
                    db.add(AuditLog(action="created", entity="ShiftMark", entity_id=emp_m.id, detail=f"multi {tg_chat_id}:{tg_msg_id} emp {emp_m.full_name}"))
            ml.employee_id = None  # множественная
            ml.shift_date = shift_date
            if created:
                ml.status = ProcessingStatus.accepted.value
                ml.reason = f"автоматически {len(created)} сотрудников с одного фото: {', '.join(created)} на {shift_date} | {window_reason} | {', '.join(['дубль '+n for n in dups]) if dups else ''}"
            else:
                ml.status = ProcessingStatus.duplicate.value
                ml.reason = f"все {len(multi)} уже отмечены дубликаты: {', '.join(dups)}"
            db.add(ml)
            await db.commit()
            # планируем перепроверку через час для каждой отметки
            for name in created:
                try:
                    task = asyncio.create_task(schedule_recheck(tg_chat_id, tg_msg_id, name, shift_date, caption))
                    _recheck_tasks.add(task)
                    task.add_done_callback(_recheck_tasks.discard)
                except Exception as e:
                    log.debug(f"schedule_recheck failed: {e}")
            return ml
        else:
            ml.status = ProcessingStatus.manual.value
            ml.reason = f"в подписи несколько сотрудников ({', '.join([e.full_name for e in multi])}) -> ручная проверка"
            ml.shift_date = None
            db.add(ml)
            await db.commit()
            return ml

    emp, ident_reason = await identify_employee(db, caption, from_user_id, from_username, group)

    if not emp:
        # Вариант А высокая активность: если у объекта фиксированный состав (allowed), и подпись не содержит никого из него — мусор → отклонено без ручной
        # Отличие: фото без подписи (caption пустая) всегда ручная, а мусор с белибердой — отклонено
        if group and group.allowed_employee_ids and caption.strip():
            try:
                allowed_set = set(json.loads(group.allowed_employee_ids))
            except:
                allowed_set = set()
            if allowed_set:
                # есть ли хоть один allowed в подписи (фаззи 0.78)
                has_allowed = False
                for e in all_emps:
                    if e.id not in allowed_set:
                        continue
                    # проверяем точное + фаззи
                    names = [e.full_name.lower()]
                    try:
                        aliases = json.loads(e.aliases) if e.aliases else []
                    except:
                        aliases = []
                    names.extend([a.lower() for a in aliases if a])
                    for n in names:
                        if not n: continue
                        import re
                        if re.search(rf'\b{re.escape(n)}\b', (caption or "").lower()):
                            has_allowed = True
                            break
                        if fuzzy_ratio((caption or "").lower(), n) >= 0.86:
                            has_allowed = True
                            break
                        # токены
                        tokens = re.findall(r"[a-zа-яё0-9]+", (caption or "").lower())
                        for tok in tokens:
                            if fuzzy_ratio(tok, n) >= 0.86:
                                has_allowed = True
                                break
                        if has_allowed: break
                    if has_allowed: break
                if not has_allowed:
                    ml.status = ProcessingStatus.rejected.value
                    ml.reason = f"мусор для объекта: подпись '{caption}' не содержит фамилии из состава ({len(allowed_set)} чел) -> отклонено без ручной (высокая активность А)"
                    db.add(ml)
                    await db.commit()
                    return ml
        # Высокая активность: если фото без человека и без подписи/белиберда — нерелевант → отклонено
        if ml.vision_person_detected is False:
            ml.status = ProcessingStatus.rejected.value
            ml.reason = f"нерелевантное фото: {ident_reason} + нет человека | caption='{caption}'"
            db.add(ml)
            await db.commit()
            return ml
        ml.status = ProcessingStatus.manual.value
        ml.reason = f"сотрудник не определен: {ident_reason}. Подпись='{caption}', TG={from_user_id}/{from_username}"
        if not caption.strip():
            ml.reason += " | фото без подписи -> ручная проверка (ТЗ 9)"
        db.add(ml)
        await db.commit()
        return ml

    ml.employee_id = emp.id

    # §6: флаг объекта "нельзя автоотметить" — даже при всех условиях уходит на ручную
    if facility and not getattr(facility, "auto_mark_allowed", True):
        # сохраняем shift_date для ручной, но не ставим авто
        shift_date, within_window, window_reason = resolve_shift_date(dt_for_shift, facility, settings.timezone)
        ml.shift_date = shift_date
        ml.status = ProcessingStatus.manual.value
        ml.reason = f"объект {facility.name} помечен 'без автоотметки' -> ручная проверка | сотрудник {emp.full_name} | {window_reason} | {vision_reason}"
        db.add(ml)
        await db.commit()
        return ml

    # 4. определить дату смены и проверить окно (используем dt_for_shift если OCR нашел дату)
    shift_date, within_window, window_reason = resolve_shift_date(dt_for_shift, facility, settings.timezone)
    ml.shift_date = shift_date
    if facility:
        ml.facility_id = facility.id

    # если вне окна - не ставим автомат, на ручную проверку (ТЗ 7,9)
    if not within_window:
        ml.status = ProcessingStatus.manual.value
        ml.reason = f"сотрудник {emp.full_name} определен ({ident_reason}), но {window_reason} -> ручная проверка"
        db.add(ml)
        await db.commit()
        return ml

    # 5. проверки дублей и постановка 1 (ТЗ 8.2)
    # а) проверка дубля по сообщению уже выше
    # б) проверка дубля по сотруднику+дате+объекту
    dup_q = await db.execute(select(ShiftMark).where(
        ShiftMark.employee_id==emp.id,
        ShiftMark.shift_date==shift_date,
        ShiftMark.facility_id== (facility.id if facility else None)
    ))
    existing_mark = dup_q.scalar_one_or_none()
    if existing_mark:
        ml.status = ProcessingStatus.duplicate.value
        ml.reason = f"дубликат: у {emp.full_name} на {shift_date} уже стоит 1 (mark_id={existing_mark.id}) -> зафиксировано в журнале"
        db.add(ml)
        await db.commit()
        # аудит
        db.add(AuditLog(action="duplicate", entity="MessageLog", entity_id=ml.id, detail=ml.reason))
        await db.commit()
        return ml

    # 6. проставить 1
    # есть ли группа-объект mismatch? просто ставим
    mark = ShiftMark(
        employee_id=emp.id,
        shift_date=shift_date,
        facility_id=facility.id if facility else None,
        value=1,
        message_id=f"{tg_chat_id}:{tg_msg_id}",
        auto_created=True,
    )
    db.add(mark)
    ml.status = ProcessingStatus.accepted.value
    ml.reason = f"автоматически проставлено 1 для {emp.full_name} на {shift_date} | {ident_reason} | {window_reason}"
    db.add(ml)
    await db.flush()
    db.add(AuditLog(action="created", entity="ShiftMark", entity_id=mark.id, detail=f"auto 1 by msg {tg_chat_id}:{tg_msg_id} emp {emp.full_name} date {shift_date}"))
    await db.commit()
    # перепроверка через час (редактирование/удаление)
    try:
        log.warning(f"scheduling recheck for {tg_chat_id}:{tg_msg_id} {emp.full_name} in {settings.recheck_delay_seconds}s")
        task = asyncio.create_task(schedule_recheck(tg_chat_id, tg_msg_id, emp.full_name, shift_date, caption))
        _recheck_tasks.add(task)
        task.add_done_callback(_recheck_tasks.discard)
    except Exception as e:
        log.warning(f"schedule_recheck failed: {e}")
    return ml

async def review_message(db: AsyncSession, message_log_id: int, decision: str, employee_id: int = None, shift_date: date = None, reviewer: str="operator"):
    """
    Ручная проверка: decision = accepted|rejected
    """
    ml = await db.get(MessageLog, message_log_id)
    if not ml:
        raise ValueError("MessageLog not found")
    if decision == "accepted":
        if not employee_id:
            employee_id = ml.employee_id
        if not shift_date:
            shift_date = ml.shift_date
        if not employee_id or not shift_date:
            raise ValueError("need employee and shift_date for accept")
        # проверка дубля
        q = await db.execute(select(ShiftMark).where(ShiftMark.employee_id==employee_id, ShiftMark.shift_date==shift_date))
        ex = q.scalar_one_or_none()
        if ex:
            ml.status = ProcessingStatus.duplicate.value
            ml.reason += f" | ручная проверка: дубликат от {reviewer}"
        else:
            mark = ShiftMark(employee_id=employee_id, shift_date=shift_date, facility_id=ml.facility_id, value=1, message_id=f"{ml.tg_chat_id}:{ml.tg_message_id}", auto_created=False)
            db.add(mark)
            ml.status = ProcessingStatus.accepted.value
            ml.reason += f" | подтверждено вручную {reviewer}"
            db.add(AuditLog(action="manual_accept", entity="MessageLog", entity_id=ml.id, detail=f"by {reviewer}"))
        ml.employee_id = employee_id
        ml.shift_date = shift_date
    elif decision == "rejected":
        ml.status = ProcessingStatus.rejected.value
        ml.reason += f" | отклонено вручную {reviewer}"
        db.add(AuditLog(action="manual_reject", entity="MessageLog", entity_id=ml.id, detail=f"by {reviewer}"))
    await db.commit()
    return ml
