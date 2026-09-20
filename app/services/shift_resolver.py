from datetime import datetime, date, time
import zoneinfo
from typing import Tuple, Optional
from ..models import Facility

def parse_windows(s: str):
    """ '06:00-12:00,18:00-22:00' -> list[(time,time)] """
    res = []
    for part in (s or "").split(","):
        part = part.strip()
        if not part or "-" not in part:
            continue
        a, b = part.split("-", 1)
        try:
            h1, m1 = map(int, a.strip().split(":"))
            h2, m2 = map(int, b.strip().split(":"))
            res.append((time(h1,m1), time(h2,m2)))
        except:
            continue
    return res or [(time(6,0), time(12,0))]

def resolve_shift_date(
    msg_dt: datetime,
    facility: Optional[Facility],
    tz_name: str = "Europe/Moscow",
) -> Tuple[date, bool, str]:
    """
    Определяет дату смены по времени сообщения и правилам.
    Возвращает (shift_date, within_window, reason)
    - Учитывает midnight_cutoff: если сообщение 00:00-04:00, относим к предыдущему дню
    - Проверяет попадание в time_windows
    """
    tz = zoneinfo.ZoneInfo(tz_name)
    # msg_dt может быть naive (UTC) или aware
    if msg_dt.tzinfo is None:
        # считаем что уже в нужной TZ если naive; но бот присылает UTC aware
        local = msg_dt.replace(tzinfo=tz)
    else:
        local = msg_dt.astimezone(tz)

    local_time = local.time()
    local_date = local.date()

    cutoff = facility.midnight_cutoff_hour if facility else 4
    if local_time.hour < cutoff:
        # относим к предыдущему дню
        from datetime import timedelta
        shift_date = local_date - timedelta(days=1)
        cutoff_reason = f"время {local_time} < cutoff {cutoff}:00 -> дата {shift_date} (пред.день)"
    else:
        shift_date = local_date
        cutoff_reason = f"дата смены = дата сообщения {shift_date}"

    windows = parse_windows(facility.time_windows if facility else "06:00-12:00")
    within = any(s <= local_time <= e for s, e in windows)
    # учет окон через полночь не нужен в MVP; но можно расширить
    # если окно типа 22:00-02:00, логика другая - пока не требуется

    if within:
        reason = f"{cutoff_reason}; время {local_time} входит в окно {windows}"
    else:
        reason = f"{cutoff_reason}; ВНЕ окна {windows} -> требует проверки"

    return shift_date, within, reason
