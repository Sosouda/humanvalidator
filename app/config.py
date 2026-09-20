from pydantic_settings import BaseSettings
from pathlib import Path

class Settings(BaseSettings):
    bot_token: str = ""
    database_url: str = "sqlite+aiosqlite:///./data/app.db"
    admin_user: str = "admin"
    admin_pass: str = "admin123"
    timezone: str = "Europe/Moscow"
    secret_key: str = "change-me-in-prod-32chars-min"
    # performance
    worker_concurrency: int = 10
    queue_maxsize: int = 10000
    # vision §2: нейронка детекции человека + OCR даты с фото — раздельные API
    vision_enabled: bool = True
    vision_strict: bool = False  # если True — без байт фото уходит на ручную
    vision_model_path: str = ""
    # API нейросети детекции человека (YOLO/InsightFace/Roboflow/HF)
    vision_person_api_url: str = ""  # напр. https://api.roboflow.com/yolov8/detect или https://api-inference.huggingface.co/models/ultralytics/yolov8n
    vision_person_api_key: str = ""
    vision_person_model: str = "yolov8n-person"
    # API нейросети для даты/времени с фото (OCR: Google Vision/Azure/PaddleOCR/LLM)
    vision_datetime_api_url: str = ""  # напр. https://vision.googleapis.com/v1/images:annotate или https://api.openai.com/v1/chat/completions
    vision_datetime_api_key: str = ""
    vision_datetime_model: str = "ocr-ru"
    # Алерты и перепроверка §9
    admin_chat_id: int = 0  # куда слать алерты об удалении/редактировании (0 = только лог)
    recheck_delay_seconds: int = 3600  # через час после 1 перепроверить
    recheck_fuzzy_threshold: float = 0.88  # ниже — считаем изменено
    # Health-check §9 вариант В: простой 5 мин в окно смены, 20-25 вне окна
    health_check_interval: int = 60  # проверять каждую минуту
    health_critical_shift_seconds: int = 300  # 5 мин когда идет смена
    health_critical_idle_seconds: int = 1200  # 20 мин когда смены не меняются
    health_fail_threshold: int = 3  # сколько подряд fails считать дауном

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"

settings = Settings()

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
