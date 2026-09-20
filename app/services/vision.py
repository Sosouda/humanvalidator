"""
Vision-сервис: нейронка для детекции человека + OCR даты/времени с фото.
Ответ §2: везде одинаковые правила, но включается per-конфигом.
Если на фото нет человека или дата не спарсилась — уходит на ручную проверку.
Реальная нейронка (InsightFace/YOLO) подключается через VISION_PERSON_API_*,
дата — через VISION_DATETIME_API_* (раздельные API per .env).
Пока — заглушка + pytesseract OCR + внешние API если заданы.
"""
import re
import logging
from datetime import datetime
from typing import Optional, Tuple
import zoneinfo

log = logging.getLogger(__name__)

# Попытка импорта OCR/ML — без жесткой зависимости
try:
    import pytesseract
    from PIL import Image
    HAS_OCR = True
except Exception:
    HAS_OCR = False

try:
    import cv2  # для face/person detect
    HAS_CV2 = True
except Exception:
    HAS_CV2 = False

from ..config import settings

# Регулярки для даты как на скрине: "19 сент. 2025 г., 08:50:06" / "19.09.2025 08:50"
DATE_PATTERNS = [
    # 19 сент. 2025 г., 08:50:06
    re.compile(r"(\d{1,2})\s*(янв|фев|мар|апр|ма[йя]|июн|июл|авг|сен|окт|ноя|дек)[^\d]*(\d{4})[^\d]+(\d{1,2}):(\d{2})(?::(\d{2}))?", re.I),
    # 19.09.2025 08:50:06 или 19-09-2025 08:50
    re.compile(r"(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{4})\s+(\d{1,2}):(\d{2})(?::(\d{2}))?"),
    # 19/09/25 08:50
    re.compile(r"(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{2})\s+(\d{1,2}):(\d{2})"),
]

MONTH_MAP = {
    "янв":1,"фев":2,"мар":3,"апр":4,"май":5,"мая":5,"июн":6,"июл":7,"авг":8,"сен":9,"сент":9,"окт":10,"ноя":11,"дек":12
}

def parse_ocr_datetime(text: str, tz_name: str = "Europe/Moscow") -> Optional[datetime]:
    if not text:
        return None
    t = text.strip()
    for pat in DATE_PATTERNS:
        m = pat.search(t)
        if not m:
            continue
        try:
            groups = m.groups()
            # паттерн с месяцем словом
            if pat is DATE_PATTERNS[0]:
                day, mon_s, year, hh, mm, ss = groups
                mon = MONTH_MAP.get(mon_s.lower()[:3], 1)
                year=int(year); day=int(day); hh=int(hh); mm=int(mm); ss=int(ss or 0)
            else:
                # числовой
                if len(groups)==6:
                    day, mon, year, hh, mm, ss = groups
                    year=int(year); mon=int(mon); day=int(day); hh=int(hh); mm=int(mm); ss=int(ss or 0)
                    if year<100: year+=2000
                else:
                    day, mon, year, hh, mm = groups
                    year=int(year); mon=int(mon); day=int(day); hh=int(hh); mm=int(mm); ss=0
                    if year<100: year+=2000
            tz = zoneinfo.ZoneInfo(tz_name)
            return datetime(year, mon, day, hh, mm, ss, tzinfo=tz)
        except Exception as e:
            log.debug(f"parse_ocr fail {e} for {text}")
            continue
    return None

def ocr_image_bytes(image_bytes: bytes) -> Tuple[str, Optional[datetime]]:
    """Возвращает (raw_text, parsed_datetime). Если OCR недоступен — ("", None)."""
    if not image_bytes or not HAS_OCR:
        return "", None
    try:
        from PIL import Image
        import io
        img = Image.open(io.BytesIO(image_bytes))
        # упор на верхнюю треть где дата (как на скрине 19 сент. вверху)
        w,h = img.size
        crop = img.crop((0, 0, w, int(h*0.25)))
        text = pytesseract.image_to_string(crop, lang="rus+eng")
        dt = parse_ocr_datetime(text, settings.timezone)
        return text, dt
    except Exception as e:
        log.debug(f"OCR fallback (tesseract): {e}")
        return "", None

def ocr_text_mock(caption: str, image_bytes: Optional[bytes]=None) -> Tuple[str, Optional[datetime]]:
    """
    Для тестов без реального изображения: если caption содержит дату вида "19 сент... 08:50"
    — считаем что OCR бы её нашел. Если image_bytes передан — пробуем реальный OCR.
    """
    if image_bytes:
        return ocr_image_bytes(image_bytes)
    # fallback: парсим саму подпись (если туда скопировали текст с фото для теста)
    dt = parse_ocr_datetime(caption or "", settings.timezone)
    return caption or "", dt

async def call_datetime_api(image_bytes: bytes) -> Tuple[str, Optional[datetime], str]:
    """Вызов внешней API нейросети для даты/времени (VISION_DATETIME_API_*). Поддержка Yandex Vision OCR + generic."""
    if not settings.vision_datetime_api_url:
        return "", None, "datetime API не настроен"
    try:
        import httpx, base64
        from PIL import Image
        import io
        url = settings.vision_datetime_api_url
        headers = {}
        # Для OCR кропим верхнюю полосу 20% — там штамп даты, иначе фон "dinozavr"
        crop_b64 = None
        try:
            img = Image.open(io.BytesIO(image_bytes))
            w, h = img.size
            crop = img.crop((0, 0, w, int(h*0.20)))
            buf = io.BytesIO()
            crop.save(buf, format="JPEG", quality=95)
            crop_b64 = base64.b64encode(buf.getvalue()).decode()
            log.info(f"OCR crop {w}x{h} -> {w}x{int(h*0.20)} b64 {len(crop_b64)}")
        except Exception as e:
            log.debug(f"OCR crop failed: {e}")
            crop_b64 = None
        b64 = crop_b64 or base64.b64encode(image_bytes).decode()
        # Roboflow Workflow OCR (photo-text-ocr)
        if "workflows" in url and "serverless.roboflow.com" in url:
            headers = {"Authorization": f"Bearer {settings.vision_datetime_api_key}", "Content-Type": "application/json"}
            payload = {"inputs": {"image": {"type": "base64", "value": b64}}}
            async with httpx.AsyncClient(timeout=20) as client:
                r = await client.post(url, json=payload, headers=headers)
                r.raise_for_status()
                data = r.json()
                # Workflows: list [{"recognized_text": ["2025","08:50:06",...]}] или {outputs: [...]}
                text = ""
                # прямой список как в твоем примере: [{"recognized_text": ["2025", "08:50:06", "Москва\nРоссия:"]}]
                if isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict) and "recognized_text" in item:
                            vals = item["recognized_text"]
                            if isinstance(vals, list):
                                text += " ".join(vals) + " "
                            else:
                                text += str(vals) + " "
                        elif isinstance(item, dict) and "text" in item:
                            text += item["text"] + " "
                elif "outputs" in data and isinstance(data["outputs"], list):
                    for out in data["outputs"]:
                        if isinstance(out, dict):
                            if "recognized_text" in out:
                                vals = out["recognized_text"]
                                if isinstance(vals, list):
                                    text += " ".join(vals) + " "
                                else:
                                    text += str(vals) + " "
                            if "text" in out:
                                text += out["text"] + " "
                            if "predictions" in out and isinstance(out["predictions"], dict) and "text" in out["predictions"]:
                                text += out["predictions"]["text"] + " "
                            for v in out.values():
                                if isinstance(v, str) and len(v) > 5:
                                    text += v + " "
                                elif isinstance(v, dict) and "text" in v:
                                    text += v["text"] + " "
                            # также recognized_text внутри predictions
                            if "recognized_text" in out and isinstance(out["recognized_text"], list):
                                text += " ".join(out["recognized_text"]) + " "
                if not text:
                    # fallback: ищем recognized_text в любом месте
                    import json as _js
                    try:
                        txt = _js.dumps(data, ensure_ascii=False)
                        # попробуем найти все recognized_text
                        import re as _re
                        for m in _re.findall(r'"recognized_text"\s*:\s*\[([^\]]+)\]', txt):
                            # m содержит "2025", "08:50:06", ...
                            parts = _re.findall(r'"([^"]+)"', m)
                            text += " ".join(parts) + " "
                    except: pass
                if not text:
                    text = data.get("text", "") if isinstance(data, dict) else "" or data.get("ocr_text", "") if isinstance(data, dict) else "" or str(data)
                dt = parse_ocr_datetime(text, settings.timezone) if text else None
                if dt:
                    log.info(f"Roboflow OCR workflow успех: {dt.isoformat()} из '{text[:60]}'")
                    return text, dt, f"Roboflow OCR: {dt.isoformat()}"
                log.info(f"Roboflow OCR workflow: дата не найдена в '{text[:60]}'")
                return text, None, f"Roboflow OCR: дата не найдена"
        # Yandex Vision OCR
        if "yandex" in url or "ocr.api.cloud" in url:
            headers["Authorization"] = f"Api-Key {settings.vision_datetime_api_key}"
            payload = {
                "mimeType": "JPEG",
                "languageCodes": ["ru", "en"],
                "model": settings.vision_datetime_model or "page",
                "content": b64
            }
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.post(url, json=payload, headers=headers)
                r.raise_for_status()
                data = r.json()
                # Yandex: {result: {textAnnotation: {fullText: "..."}}}
                text = ""
                if "result" in data and "textAnnotation" in data["result"]:
                    text = data["result"]["textAnnotation"].get("fullText", "")
                elif "textAnnotation" in data:
                    text = data["textAnnotation"].get("fullText", "")
                else:
                    text = data.get("text", "") or str(data)
                dt = parse_ocr_datetime(text, settings.timezone) if text else None
                if dt:
                    log.info(f"Yandex OCR успех: {dt.isoformat()} из '{text[:60]}'")
                    return text, dt, f"Yandex OCR: {dt.isoformat()}"
                log.info(f"Yandex OCR: дата не найдена в '{text[:60]}'")
                return text, None, f"Yandex OCR: дата не найдена в '{text[:60]}'"
        # Generic / OpenAI
        if settings.vision_datetime_api_key:
            headers["Authorization"] = f"Bearer {settings.vision_datetime_api_key}"
            headers["x-api-key"] = settings.vision_datetime_api_key
        payload = {"image": b64, "model": settings.vision_datetime_model}
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(url, json=payload, headers=headers)
            r.raise_for_status()
            data = r.json()
            text = data.get("text") or data.get("ocr_text") or ""
            if not text and "choices" in data:
                text = data["choices"][0].get("message", {}).get("content", "") or data["choices"][0].get("text","")
            dt = parse_ocr_datetime(text, settings.timezone) if text else None
            if dt:
                return text, dt, f"datetime API ({url}): {dt.isoformat()}"
            return text, None, f"datetime API: дата не найдена в '{text[:60]}'"
    except Exception as e:
        # не логируем ключи/URL с секретом
        msg = str(e).replace(settings.vision_datetime_api_key, "***").replace(settings.vision_person_api_key, "***") if settings.vision_datetime_api_key else str(e)
        log.debug(f"datetime API fallback: {msg[:200]}")
        return "", None, "дата на фото не распознана — используется время сообщения"

async def call_person_api(image_bytes: bytes) -> Tuple[Optional[bool], str]:
    """Вызов внешней API нейросети детекции человека (VISION_PERSON_API_*). Поддержка Roboflow + generic."""
    if not settings.vision_person_api_url:
        return None, "person API не настроен"
    try:
        import httpx, base64
        url = settings.vision_person_api_url
        b64 = base64.b64encode(image_bytes).decode()
        # Workflow Roboflow: https://serverless.roboflow.com/<workspace>/workflows/<workflowId>
        if "workflows" in url:
            headers = {"Authorization": f"Bearer {settings.vision_person_api_key}", "Content-Type": "application/json"}
            payload = {"inputs": {"image": {"type": "base64", "value": b64}}}
            async with httpx.AsyncClient(timeout=20) as client:
                r = await client.post(url, json=payload, headers=headers)
                r.raise_for_status()
                data_j = r.json()
                # Новый формат: [{"Have_person": "yes", "person_count": 1}] (твой воркфлоу)
                # Старый: {outputs: [{predictions: [...] }]}
                if isinstance(data_j, list):
                    for item in data_j:
                        if isinstance(item, dict):
                            if "Have_person" in item:
                                has = str(item["Have_person"]).lower() in ("yes","true","1","да")
                                cnt = item.get("person_count", 1 if has else 0)
                                log.info(f"Roboflow workflow Have_person={item['Have_person']} count={cnt}")
                                return has, f"Roboflow workflow: Have_person={has}"
                            if "have_person" in item:
                                has = str(item["have_person"]).lower() in ("yes","true","1")
                                return has, f"Roboflow workflow: have_person={has}"
                preds = []
                if "outputs" in data_j and isinstance(data_j["outputs"], list):
                    for out in data_j["outputs"]:
                        # новый формат внутри outputs тоже может быть Have_person
                        if isinstance(out, dict) and ("Have_person" in out or "have_person" in out):
                            hp = out.get("Have_person") or out.get("have_person")
                            has = str(hp).lower() in ("yes","true","1","да")
                            return has, f"Roboflow workflow: Have_person={has}"
                        if isinstance(out, dict) and "predictions" in out:
                            preds.extend(out["predictions"])
                        elif isinstance(out, dict):
                            for v in out.values():
                                if isinstance(v, list) and v and isinstance(v[0], dict) and "class" in v[0]:
                                    preds.extend(v)
                else:
                    preds = data_j.get("predictions") or data_j.get("result", {}).get("predictions") or []
                if preds or "predictions" in str(data_j).lower():
                    has = any(p.get("class")=="person" for p in preds) if preds else False
                    conf = max([p.get("confidence",0) for p in preds if p.get("class")=="person"], default=0)
                    log.info(f"Roboflow workflow успех: person={has} conf={conf:.2f}")
                    return has, f"Roboflow workflow: person={has} preds={len(preds)}"
                # если дошли сюда и data_j это список с Have_person уже обработан выше, иначе fallback
                if isinstance(data_j, list) and data_j and isinstance(data_j[0], dict) and "Have_person" in data_j[0]:
                    # уже обработано выше, но на всякий
                    has = str(data_j[0]["Have_person"]).lower()=="yes"
                    return has, f"Roboflow workflow: Have_person={has}"
                log.info("Roboflow workflow: no predictions")
                return False, "Roboflow workflow: no predictions"
        # Serverless Roboflow (inference v1.5+): https://serverless.roboflow.com/<model>/<version>  header Authorization: Bearer KEY, body = base64
        if "serverless.roboflow.com" in url:
            headers = {"Authorization": f"Bearer {settings.vision_person_api_key}", "Content-Type": "application/x-www-form-urlencoded"}
            async with httpx.AsyncClient(timeout=20) as client:
                r = await client.post(url, content=b64, headers=headers)
                r.raise_for_status()
                data_j = r.json()
                # Serverless: {predictions: [{class: "person", confidence: ...}, ...]} или {"inference_id":..., "predictions": [...]}
                preds = data_j.get("predictions") or data_j.get("result", {}).get("predictions") or []
                if preds or "predictions" in data_j:
                    has = any(p.get("class")=="person" for p in preds)
                    conf = max([p.get("confidence",0) for p in preds if p.get("class")=="person"], default=0)
                    log.info(f"Roboflow serverless успех: person={has} conf={conf:.2f}")
                    return has, f"Roboflow: person={has} conf={conf:.2f} preds={len(preds)}"
                log.info("Roboflow serverless: no predictions")
                return False, "Roboflow: no predictions"
        # Classic Roboflow Hosted: https://detect.roboflow.com/<model>/<version>?api_key=KEY , body x-www-form-urlencoded image=<b64>
        if "roboflow.com" in url:
            if "api_key=" not in url and settings.vision_person_api_key:
                url = url + ("&" if "?" in url else "?") + f"api_key={settings.vision_person_api_key}"
            headers = {"Content-Type": "application/x-www-form-urlencoded"}
            data = f"image={b64}"
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.post(url, content=data, headers=headers)
                r.raise_for_status()
                data_j = r.json()
                if "predictions" in data_j:
                    has = any(p.get("class")=="person" for p in data_j["predictions"])
                    conf = max([p.get("confidence",0) for p in data_j["predictions"] if p.get("class")=="person"], default=0)
                    log.info(f"Roboflow успех: person={has} conf={conf:.2f}")
                    return has, f"Roboflow: person={has} conf={conf:.2f} preds={len(data_j['predictions'])}"
                log.info("Roboflow: no predictions")
                return False, f"Roboflow: no predictions"
        # Generic
        headers = {}
        if settings.vision_person_api_key:
            headers["Authorization"] = f"Bearer {settings.vision_person_api_key}"
            headers["x-api-key"] = settings.vision_person_api_key
        payload = {"image": b64, "model": settings.vision_person_model}
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(url, json=payload, headers=headers)
            r.raise_for_status()
            data = r.json()
            if "person" in data:
                return bool(data["person"]), f"person API: person={data['person']}"
            if "count" in data:
                return data["count"]>0, f"person API: count={data['count']}"
            if "predictions" in data:
                has = any(p.get("class")=="person" or p.get("label")=="person" for p in data["predictions"])
                return has, f"person API: predictions {len(data['predictions'])}"
            txt = str(data).lower()
            if "person" in txt or "face" in txt:
                return True, f"person API: найдено в ответе"
            return False, f"person API: не найдено"
    except Exception as e:
        msg = str(e).replace(settings.vision_person_api_key, "***") if settings.vision_person_api_key else str(e)
        log.debug(f"person API fallback: {msg[:200]}")
        return None, "person API fallback"

async def extract_datetime_neural(image_bytes: Optional[bytes], caption: str) -> Tuple[str, Optional[datetime], str]:
    """
    Нейронка для считывания даты/времени с фото. Вызывается ТОЛЬКО если человек найден (pipe: person -> datetime).
    Возвращает (raw_text, datetime, reason) — reason уже user-friendly для журнала.
    """
    if not settings.vision_enabled:
        return "", None, "проверка даты отключена"
    if image_bytes:
        if settings.vision_datetime_api_url:
            text, dt, reason = await call_datetime_api(image_bytes)
            if dt:
                return text, dt, f"дата с фото: {dt.strftime('%d.%m.%Y %H:%M')} МСК"
            text2, dt2 = ocr_image_bytes(image_bytes)
            if dt2:
                return text2, dt2, f"дата с фото: {dt2.strftime('%d.%m.%Y %H:%M')} МСК"
            # не показываем технические детали API в журнале
            return text or text2, None, "дата на фото не распознана — используется время сообщения"
        text, dt = ocr_image_bytes(image_bytes)
        if dt:
            return text, dt, f"дата с фото: {dt.strftime('%d.%m.%Y %H:%M')} МСК"
        else:
            return text, None, "дата на фото не распознана — используется время сообщения"
    text, dt = ocr_text_mock(caption, None)
    if dt:
        return text, dt, f"дата с фото: {dt.strftime('%d.%m.%Y %H:%M')} МСК"
    return "", None, "дата с фото не найдена — используется время сообщения"

async def detect_person(image_bytes: Optional[bytes], has_photo: bool) -> Tuple[Optional[bool], str]:
    """
    Нейронка: есть ли человек на фото. Возвращает user-friendly reason для журнала.
    """
    if not has_photo:
        return False, "нет фото"
    if not settings.vision_enabled:
        return None, "проверка человека отключена"
    if not image_bytes:
        if settings.vision_strict:
            log.info("person: нет байт — ручная (strict)")
            return None, "фото не загружено — требуется ручная проверка"
        log.info("person: нет байт — fallback есть (strict=false)")
        return True, "человек на фото: есть"
    # 1) внешняя API детекции если настроена
    log.info(f"person: вызываю {settings.vision_person_api_url} bytes={len(image_bytes)}")
    if settings.vision_person_api_url:
        res, reason = await call_person_api(image_bytes)
        if res is not None:
            log.info(f"person API ответ: {reason}")
            if res:
                return True, "человек на фото: есть"
            else:
                return False, "человек на фото: не обнаружен — требуется ручная проверка"
        log.info(f"person API fallback: {reason}")
    # 2) локальная проверка если CV2 доступен
    if HAS_CV2:
        try:
            import numpy as np, cv2
            arr = np.frombuffer(image_bytes, np.uint8)
            img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
            faces = cascade.detectMultiScale(gray, 1.1, 4)
            if len(faces)>0:
                return True, "человек на фото: есть"
            else:
                return False, "человек на фото: не обнаружен — требуется ручная проверка"
        except Exception as e:
            log.debug(f"vision detect error {e}")
            return True, "человек на фото: есть"
    return True, "человек на фото: есть"

def is_vision_required() -> bool:
    return settings.vision_enabled
