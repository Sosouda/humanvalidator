"""
Health-check вариант В: 5 мин критично в окно смены, 20-25 вне окна.
Проверяет bot.get_me каждую минуту, при 3 подряд fails — считает даун.
Если даун длится > критичного порога — алерт в admin_chat_id и лог.
Порог выбирается по текущему времени: попадает ли now в time_windows любого Facility.
"""
import asyncio, logging
from datetime import datetime, time
import zoneinfo
from sqlalchemy import select
from ..config import settings
from ..database import SessionLocal
from ..models import Facility
from .vision import parse_ocr_datetime  # reuse

log = logging.getLogger(__name__)

def _now_in_shift_window(now: datetime, facilities) -> bool:
    """true если сейчас внутри окна любого объекта (смена идет)"""
    t = now.time()
    for fac in facilities:
        s = fac.time_windows or "06:00-12:00"
        for part in s.split(","):
            part=part.strip()
            if "-" not in part: continue
            a,b = part.split("-",1)
            try:
                h1,m1 = map(int, a.strip().split(":"))
                h2,m2 = map(int, b.strip().split(":"))
                start=time(h1,m1); end=time(h2,m2)
                # окна не через полночь в MVP
                if start <= t <= end:
                    return True
            except: continue
    return False

async def health_check_loop():
    if not settings.bot_token or settings.bot_token=="your_bot_token_here":
        log.info("health-check: BOT_TOKEN пустой — пропуск")
        return
    from telegram import Bot
    bot = Bot(token=settings.bot_token)
    tz = zoneinfo.ZoneInfo(settings.timezone)
    fails = 0
    down_since = None
    alerted = False
    log.info(f"health-check старт: interval={settings.health_check_interval}s shift_crit={settings.health_critical_shift_seconds}s idle_crit={settings.health_critical_idle_seconds}s")
    while True:
        await asyncio.sleep(settings.health_check_interval)
        # определяем критичный порог на сейчас
        try:
            async with SessionLocal() as db:
                facs = (await db.execute(select(Facility))).scalars().all()
                now = datetime.now(tz)
                in_shift = _now_in_shift_window(now, facs)
                crit = settings.health_critical_shift_seconds if in_shift else settings.health_critical_idle_seconds
        except Exception as e:
            log.debug(f"health: failed to get facilities {e}")
            in_shift=False
            crit = settings.health_critical_idle_seconds

        # пингуем Telegram
        try:
            await asyncio.wait_for(bot.get_me(), timeout=10)
            # успех
            if fails >= settings.health_fail_threshold and down_since:
                # был даун, восстановился
                log.info(f"health-check: бот восстановился после {fails} fails")
                down_since=None
                alerted=False
            fails=0
            log.debug("health-check: ok")
        except Exception as e:
            fails+=1
            log.warning(f"health-check fail {fails}/{settings.health_fail_threshold}: {e}")
            if fails >= settings.health_fail_threshold and down_since is None:
                down_since = datetime.now(tz)
                log.warning(f"health-check: даун начался {down_since} in_shift={in_shift} crit={crit}s")
            if down_since:
                elapsed = (datetime.now(tz) - down_since).total_seconds()
                if not alerted and elapsed >= crit:
                    # алерт
                    where = "в окно смены" if in_shift else "вне окна смены"
                    text = f"⚠️ Бот недоступен {int(elapsed)}с {where} (порог {crit}с) — таблица может не обновляться! fails={fails} время {datetime.now(tz)}"
                    log.error(text)
                    try:
                        from .processor import send_alert
                        await send_alert(text)
                    except: pass
                    # пишем в AuditLog
                    try:
                        async with SessionLocal() as db:
                            from ..models import AuditLog
                            db.add(AuditLog(action="health_alert", entity="Bot", detail=text))
                            await db.commit()
                    except: pass
                    alerted=True
            # при лимите Telegram (429) — бэкофф, не считаем сразу критичным, но fails растет
