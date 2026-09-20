# syntax=docker/dockerfile:1.6
FROM python:3.14-slim AS base

# Безопасный non-root
RUN groupadd -r app && useradd -r -g app -m app
WORKDIR /app

# Зависимости отдельно для кэша
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Код
COPY app ./app
COPY main.py ./

# Права: data может быть смонтирована с хоста как root — делаем 777 чтобы app мог писать app.db/app.log, fallback в main.py на /tmp если нет
RUN mkdir -p data && chown -R app:app /app && chmod 777 data

USER app
EXPOSE 8000

# Health-check использует /health с проверкой БД
HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=20s \
  CMD python -c "import httpx; httpx.get('http://127.0.0.1:8000/health', timeout=3).raise_for_status()" || exit 1

# Не .env в образ — секреты через --env-file или --env
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "*"]
