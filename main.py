import asyncio
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from app.database import init_db, SessionLocal
from app.api.routes import router
from app.telegram.client import start_bot, stop_bot
from sqlalchemy import select
from app.models import Facility, Employee, TelegramGroup
from app.config import settings
import json

from pathlib import Path as _LogPath
_log_file = _LogPath(__file__).parent / "data" / "app.log"
try:
    _log_file.parent.mkdir(parents=True, exist_ok=True)
    # пробуем создать файл, если нет прав — fallback только на консоль
    _handlers = [logging.StreamHandler()]
    try:
        _handlers.append(logging.FileHandler(str(_log_file), encoding="utf-8", mode="a"))
    except PermissionError:
        # data смонтирована с хоста как root — пишем в /tmp
        _log_file = _LogPath("/tmp/app.log")
        _handlers.append(logging.FileHandler(str(_log_file), encoding="utf-8", mode="a"))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        handlers=_handlers,
    )
except Exception as e:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    logging.warning(f"FileHandler { _log_file } не доступен: {e}, логи только в консоль")
# глушим шум от polling, но оставляем INFO для vision API
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("telegram.ext.Updater").setLevel(logging.WARNING)
logging.getLogger("watchfiles.main").setLevel(logging.WARNING)
log_main = logging.getLogger("main")
log_main.info(f"Логи пишутся в консоль и в { _log_file } — открой http://127.0.0.1:8000/logs для просмотра")

bot_app = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global bot_app
    await init_db()
    await seed_demo()
    # RBAC: создаем дефолтного админа
    try:
        from app.auth import ensure_default_admin
        await ensure_default_admin()
    except Exception as e:
        logging.warning(f"ensure_default_admin failed: {e}")
    try:
        bot_app = await start_bot()
    except Exception as e:
        logging.warning(f"lifespan: bot start failed, continue without bot: {e}")
        bot_app = None
    yield
    if bot_app:
        try:
            await stop_bot(bot_app)
        except Exception as e:
            logging.warning(f"stop_bot failed: {e}")

from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

limiter = Limiter(key_func=get_remote_address, default_limits=["200/minute"])
app = FastAPI(title="TG2GSheet — авто-таблица смен", lifespan=lifespan)
app.state.limiter = limiter
app.add_middleware(SlowAPIMiddleware)

# Security headers
class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        # CSP — разрешаем inline для переключателя мобилка/десктоп (base.html)
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data: https:; connect-src 'self'"
        return response
app.add_middleware(SecurityHeadersMiddleware)

@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request, exc):
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=429, content={"detail": "Слишком много запросов, попробуйте позже"})

app.include_router(router)

# 401 -> redirect to /login for browsers
from fastapi import Request, Depends, Form, HTTPException, status, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.auth import get_current_user, verify_password, create_session_token, SESSION_COOKIE, SESSION_MAX_AGE, hash_password, require_admin, require_operator, require_observer
from app.models import User, UserRole
from sqlalchemy import select as sel2
from jinja2 import Environment, FileSystemLoader
from pathlib import Path as Path2
_tmpl_env = Environment(loader=FileSystemLoader(str(Path2(__file__).parent / "app" / "web" / "templates")))

@app.exception_handler(HTTPException)
async def http_exc_handler(request: Request, exc: HTTPException):
    if exc.status_code == 401:
        # для браузера — редирект на логин, для API — JSON
        accept = request.headers.get("accept","")
        if "text/html" in accept or request.url.path in ["/","/journal","/manual","/groups","/employees"]:
            return RedirectResponse(url="/login", status_code=303)
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

@app.get("/login", response_class=HTMLResponse)
async def login_get(request: Request):
    return HTMLResponse(_tmpl_env.get_template("login.html").render(request=request, error=None))

# Блокировка после 5 неудачных входов — защита от брутфорса
_failed_logins: dict[str, list[float]] = {}
import time as _time
def _is_locked(key: str) -> bool:
    now = _time.time()
    lst = _failed_logins.get(key, [])
    # чистим старше 15 мин
    lst = [t for t in lst if now - t < 900]
    _failed_logins[key] = lst
    return len(lst) >= 5
def _record_fail(key: str):
    _failed_logins.setdefault(key, []).append(_time.time())
def _clear_fails(key: str):
    _failed_logins.pop(key, None)

@app.post("/login", response_class=HTMLResponse)
@limiter.limit("10/minute")
async def login_post(request: Request, username: str = Form(...), password: str = Form(...), db: AsyncSession = Depends(get_db)):
    # ключ — IP + username, чтобы не блокировать всех
    client_ip = request.client.host if request.client else "unknown"
    lock_key = f"{client_ip}:{username}"
    if _is_locked(lock_key):
        return HTMLResponse(_tmpl_env.get_template("login.html").render(request=request, error="Слишком много неудачных попыток. Попробуйте через 15 минут."), status_code=429)
    res = await db.execute(sel2(User).where(User.username==username))
    user = res.scalar_one_or_none()
    if not user or not verify_password(password, user.password_hash) or not user.is_active:
        _record_fail(lock_key)
        return HTMLResponse(_tmpl_env.get_template("login.html").render(request=request, error="Неверный логин или пароль"), status_code=401)
    _clear_fails(lock_key)
    token = create_session_token(user.username)
    resp = RedirectResponse(url="/", status_code=303)
    is_secure = request.url.scheme == "https"
    resp.set_cookie(key=SESSION_COOKIE, value=token, max_age=SESSION_MAX_AGE, httponly=True, samesite="lax", secure=is_secure)
    return resp

@app.get("/logout")
async def logout():
    resp = RedirectResponse(url="/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE)
    return resp

# управление пользователями — только admin
@app.get("/users", response_class=HTMLResponse)
async def users_page(request: Request, db: AsyncSession = Depends(get_db), current_user: User = Depends(require_admin)):
    users = (await db.execute(sel2(User).order_by(User.username))).scalars().all()
    return HTMLResponse(_tmpl_env.get_template("users.html").render(request=request, users=users, current_user=current_user))

@app.post("/users/add")
async def users_add(request: Request, username: str = Form(...), password: str = Form(...), role: str = Form(...), db: AsyncSession = Depends(get_db), current_user: User = Depends(require_admin)):
    if role not in [UserRole.admin.value, UserRole.operator.value, UserRole.observer.value]:
        raise HTTPException(status_code=400, detail="bad role")
    res = await db.execute(sel2(User).where(User.username==username))
    if res.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="user exists")
    u = User(username=username.strip(), password_hash=hash_password(password), role=role)
    db.add(u)
    await db.commit()
    return RedirectResponse(url="/users", status_code=303)

@app.post("/users/{uid}/role")
async def users_role(request: Request, uid: int, role: str = Form(...), db: AsyncSession = Depends(get_db), current_user: User = Depends(require_admin)):
    u = await db.get(User, uid)
    if not u:
        raise HTTPException(status_code=404, detail="not found")
    if role not in [UserRole.admin.value, UserRole.operator.value, UserRole.observer.value]:
        raise HTTPException(status_code=400, detail="bad role")
    if u.role == UserRole.admin.value and role != UserRole.admin.value:
        cnt = (await db.execute(sel2(User).where(User.role==UserRole.admin.value))).scalars().all()
        if len(cnt) <= 1:
            raise HTTPException(status_code=400, detail="cannot demote last admin")
    u.role = role
    await db.commit()
    return RedirectResponse(url="/users", status_code=303)

@app.post("/users/{uid}/toggle")
async def users_toggle(request: Request, uid: int, db: AsyncSession = Depends(get_db), current_user: User = Depends(require_admin)):
    u = await db.get(User, uid)
    if not u:
        raise HTTPException(status_code=404)
    if u.id == current_user.id:
        raise HTTPException(status_code=400, detail="cannot disable self")
    u.is_active = not u.is_active
    await db.commit()
    return RedirectResponse(url="/users", status_code=303)

@app.post("/users/{uid}/delete")
async def users_delete(request: Request, uid: int, db: AsyncSession = Depends(get_db), current_user: User = Depends(require_admin)):
    u = await db.get(User, uid)
    if not u or u.id == current_user.id:
        raise HTTPException(status_code=400, detail="cannot delete self")
    await db.delete(u)
    await db.commit()
    return RedirectResponse(url="/users", status_code=303)

@app.get("/logs", response_class=HTMLResponse)
async def view_logs(request: Request, n: int = Query(300, ge=50, le=2000), db: AsyncSession = Depends(get_db), current_user: User = Depends(require_admin)):
    p = Path(__file__).parent / "data" / "app.log"
    text = ""
    if p.exists():
        try:
            lines = p.read_text(encoding="utf-8", errors="ignore").splitlines()[-n:]
            text = "\n".join(lines)
        except Exception as e:
            text = f"Ошибка чтения лога: {e}"
    else:
        text = "Лог файл еще не создан. Запусти main.py и пришли фото."
    html = f"""
    <!doctype html><html lang=ru><head><meta charset=utf-8><title>Логи</title>
    <style>body{{font-family:monospace;background:#0f172a;color:#e2e8f0;padding:20px}}pre{{background:#1e293b;padding:16px;border-radius:10px;overflow:auto;white-space:pre-wrap;word-break:break-all}}a{{color:#60a5fa}} .info{{color:#64748b;font-size:13px}}</style></head><body>
    <h2>Логи сервера — {p} (последние {n} строк)</h2>
    <p class=info>Запускай <code>python main.py</code> в видимом PowerShell чтобы видеть логи в реальном времени. Файл лога дописывается при каждом фото/API вызове.</p>
    <p><a href="/logs?n=500">500</a> | <a href="/logs?n=1000">1000</a> | <a href="/logs">обновить</a> | <a href="/">← таблица</a></p>
    <pre>{text.replace('&','&amp;').replace('<','&lt;')}</pre>
    </body></html>
    """
    return HTMLResponse(html)

# static if needed
# app.mount("/static", StaticFiles(directory=str(Path(__file__).parent/"app/web/static")), name="static")

async def seed_demo():
    async with SessionLocal() as db:
        # facilities
        facs = (await db.execute(select(Facility))).scalars().all()
        if not facs:
            f1 = Facility(name="Москва — БЦ Тургенев", time_windows="06:00-12:00,18:00-22:00", midnight_cutoff_hour=4, auto_mark_allowed=True)
            f2 = Facility(name="Объект 2 — Склад", time_windows="07:00-11:00", midnight_cutoff_hour=4, auto_mark_allowed=False)
            db.add_all([f1,f2])
            await db.flush()
            facs = [f1,f2]
        else:
            # миграция: добавим флаг если его нет
            for f in facs:
                if not hasattr(f, 'auto_mark_allowed') or f.auto_mark_allowed is None:
                    f.auto_mark_allowed = True
            await db.flush()
        # employees как в примере таблицы
        emps = (await db.execute(select(Employee))).scalars().all()
        if not emps:
            names = ["Чистов","Васильев","Македонский","Голицин","Сидоренко","Петров","Иванов","Мехоношин"]
            for n in names:
                db.add(Employee(full_name=n, aliases=json.dumps([], ensure_ascii=False)))
            await db.flush()
        # groups demo — только супергруппы/каналы для §7 (t.me/c/ ссылка)
        groups = (await db.execute(select(TelegramGroup))).scalars().all()
        if not groups:
            db.add(TelegramGroup(tg_id=-1001234567890, title="БЦ Тургенев — смена", facility_id=facs[0].id, is_enabled=True, group_type="supergroup"))
            db.add(TelegramGroup(tg_id=-1001234567891, title="Склад — ночь", facility_id=facs[1].id if len(facs)>1 else None, is_enabled=True, group_type="supergroup"))
        await db.commit()
        logging.info("seed demo done")

@app.get("/health")
async def health(db: AsyncSession = Depends(get_db)):
    # readiness: проверяем БД + бот
    try:
        await db.execute(select(Facility).limit(1))
        db_ok = True
    except Exception as e:
        return {"status":"error", "db": str(e)}
    bot_ok = bool(settings.bot_token and settings.bot_token != "your_bot_token_here")
    return {"status":"ok", "db":"ok", "bot": "configured" if bot_ok else "demo", "version":"mvp-1.0"}

if __name__ == "__main__":
    import uvicorn
    # reload только для разработки, в проде без него чтобы не спамить watchfiles
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
