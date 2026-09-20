import re
import json
import difflib
from typing import Optional, List, Tuple
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from ..models import Employee, TelegramGroup

def normalize(text: str) -> str:
    return (text or "").strip().lower()

def fuzzy_ratio(a: str, b: str) -> float:
    """0..1, используем difflib (без внешних зависимостей). Для 'Маштвков' vs 'Маштаков' ≈0.88"""
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()

def find_fuzzy(caption: str, employees: List[Employee], threshold: float = 0.78) -> List[Tuple[Employee, float, str]]:
    """
    Ищет ближайшее совпадение по всей подписи (токенизирует). Возвращает отсортированный список (emp, ratio, matched_name).
    Порог 0.78 подбирался на кейсах: Маштвков/Маштаков 0.88, мехоношин/Мехоношин 1.0, белиберда 'qwerty' <0.4.
    """
    cap = normalize(caption)
    if not cap or len(cap) < 2:
        return []
    tokens = re.findall(r"[a-zа-яё0-9]+", cap)
    best = []
    for emp in employees:
        names = [emp.full_name.lower()]
        try:
            aliases = json.loads(emp.aliases) if emp.aliases else []
        except:
            aliases = []
        names.extend([a.lower() for a in aliases if a])
        max_r = 0
        max_n = ""
        for n in names:
            n = n.strip().lower()
            if not n:
                continue
            # сравниваем с каждым токеном и целиком с подписью
            for tok in tokens:
                r = fuzzy_ratio(tok, n)
                if r > max_r:
                    max_r = r
                    max_n = n
            # также целиком (если подпись = "Маштвков")
            r_full = fuzzy_ratio(cap, n)
            if r_full > max_r:
                max_r = r_full
                max_n = n
        if max_r >= threshold:
            best.append((emp, max_r, max_n))
    best.sort(key=lambda x: x[1], reverse=True)
    return best

async def identify_employee(
    db: AsyncSession,
    caption: str,
    from_user_id: Optional[int],
    from_username: Optional[str],
    group: Optional[TelegramGroup],
    employees: List[Employee] = None,
) -> Tuple[Optional[Employee], str]:
    """
    Возвращает (employee, reason). Если неоднозначно -> None.
    Правила по ТЗ 6:
    - шаблон подписи (пока считаем что фамилия = подпись целиком как на фото)
    - упоминание фамилии/позывного/табельного в подписи
    - привязка TG пользователя
    - привязка группы к ограниченному перечню
    """
    if employees is None:
        res = await db.execute(select(Employee).where(Employee.is_active == True))
        employees = res.scalars().all()

    cap = normalize(caption)
    candidates = []

    # 1) точное совпадение фамилии/алиаса в подписи
    for emp in employees:
        names = [emp.full_name.lower()]
        try:
            aliases = json.loads(emp.aliases) if emp.aliases else []
        except:
            aliases = [a.strip() for a in emp.aliases.split(",") if a.strip()]
        names.extend([a.lower() for a in aliases])
        for n in names:
            n = n.strip().lower()
            if not n:
                continue
            if n == cap or re.search(rf'\b{re.escape(n)}\b', cap):
                candidates.append((emp, f"совпадение по имени/алиасу '{n}' в подписи"))
                break

    # 1b) фаззи-совпадение при опечатке (Маштвков→Маштаков) — если точного нет
    if not candidates and cap and len(cap) >= 3:
        fuzzy = find_fuzzy(caption, employees, threshold=0.78)
        if fuzzy:
            best_emp, best_r, best_n = fuzzy[0]
            # если лучший сильно отрывается от второго — считаем однозначным
            second_r = fuzzy[1][1] if len(fuzzy) > 1 else 0
            if best_r >= 0.86 or (best_r >= 0.78 and best_r - second_r >= 0.12):
                # белиберда типа 'qwerty asdf' даст <0.4 — не попадёт
                candidates.append((best_emp, f"фаззи-совпадение '{best_n}'≈'{cap}' ratio={best_r:.2f}"))
            elif best_r >= 0.78:
                # близко но неоднозначно — ручная
                # вернем None с причиной ниже, но сохраним инфо
                pass
            # иначе — нет кандидатов

    # 2) привязка TG пользователя
    if from_user_id:
        for emp in employees:
            if emp.telegram_user_id and emp.telegram_user_id == from_user_id:
                # если уже есть кандидат по подписи - проверяем совпадение
                # если кандидатов 0 - добавляем по TG
                # если кандидат по подписи другой сотрудник -> неоднозначность
                candidates.append((emp, f"привязка TG user_id {from_user_id}"))
                break
    if from_username:
        uname = normalize(from_username).lstrip("@")
        for emp in employees:
            if emp.telegram_username and normalize(emp.telegram_username).lstrip("@") == uname:
                candidates.append((emp, f"привязка TG username @{uname}"))
                break

    # 3) фильтр по группе (сужение)
    if group and group.allowed_employee_ids:
        try:
            allowed = set(json.loads(group.allowed_employee_ids))
        except:
            allowed = set()
        if allowed:
            filtered = [(e, r) for e, r in candidates if e.id in allowed]
            # если были кандидаты вне allowed - игнорируем их
            # если кандидатов 0 но есть allowed - не добавляем автоматически
            if candidates and not filtered:
                return None, f"кандидаты {', '.join([e.full_name for e,_ in candidates])} не входят в allowed группы {group.title}"
            candidates = filtered

    # дедупликация по employee.id
    uniq = {}
    for e, r in candidates:
        uniq[e.id] = (e, r)
    candidates = list(uniq.values())

    if len(candidates) == 0:
        # если не нашли — пробуем фаззи для диагноза (белиберда vs опечатка)
        fuzzy = find_fuzzy(caption, employees, threshold=0.70)
        if fuzzy:
            best_emp, best_r, best_n = fuzzy[0]
            if best_r >= 0.70:
                return None, f"похоже на '{best_n}' (ratio={best_r:.2f}) для '{cap}' — опечатка? Нужна ручная проверка (белиберда/неоднозначность)"
        return None, "не удалось однозначно определить сотрудника (нет совпадений)"
    if len(candidates) > 1:
        names = ", ".join([e.full_name for e,_ in candidates])
        return None, f"неоднозначность: найдено несколько сотрудников: {names} -> ручная проверка"
    emp, reason = candidates[0]
    # Если caption пустая и единственное основание - TG, считаем неоднозначным если нет фото-привязки? По ТЗ фото+подпись ideally, но допускаем TG
    # Для MVP: если caption пустая и нашли только по TG - требуем ручной проверки? Нет, считаем принятым но с пометкой.
    return emp, reason

def extract_multiple_employees(caption: str, employees: List[Employee]) -> List[Employee]:
    """Если в одной подписи указано несколько сотрудников (разделены запятой/переносом) — точное + фаззи"""
    cap = normalize(caption)
    found = []
    for emp in employees:
        names = [emp.full_name.lower()]
        try:
            aliases = json.loads(emp.aliases) if emp.aliases else []
        except:
            aliases = []
        names.extend([a.lower() for a in aliases])
        matched = False
        for n in names:
            if n and re.search(rf'\b{re.escape(n.lower())}\b', cap):
                found.append(emp)
                matched = True
                break
        if not matched:
            # фаззи на токенах
            for n in names:
                if not n: continue
                tokens = re.findall(r"[a-zа-яё0-9]+", cap)
                for tok in tokens:
                    if fuzzy_ratio(tok, n) >= 0.86:
                        found.append(emp)
                        matched = True
                        break
                if matched: break
    return found
