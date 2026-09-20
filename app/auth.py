import hashlib, hmac, secrets, logging
from datetime import datetime
from typing import Optional
from fastapi import Request, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from .database import get_db
from .models import User, UserRole
from .config import settings

log = logging.getLogger(__name__)

SESSION_COOKIE = "session"
SESSION_MAX_AGE = 60*60*24*7  # 7 дней

# SECRET должен быть независим от BOT_TOKEN, 32+ случайных байт
# В проде обязательно переопредели в .env: SECRET_KEY=$(python -c "import secrets; print(secrets.token_urlsafe(32))")
if settings.secret_key == "change-me-in-prod-32chars-min":
    log.warning("SECRET_KEY использует дефолт — сгенерируй новый: python -c \"import secrets; print(secrets.token_urlsafe(32))\" и положи в .env")
    _secret = "change-me-in-prod-32chars-min-change-me"
else:
    _secret = settings.secret_key

# Используем itsdangerous вместо ручного HMAC 16 hex (64 бит -> 256 бит)
_serializer = URLSafeTimedSerializer(_secret, salt="tg2gsheet-session")

def hash_password(password: str, salt: str = None) -> str:
    if salt is None:
        salt = secrets.token_hex(16)  # 128 бит соли
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200000).hex()
    return f"{salt}${h}"

def verify_password(password: str, stored: str) -> bool:
    try:
        salt, h = stored.split("$", 1)
        # поддержка старых хешей (8 hex + 100k) и новых (16 hex + 200k)
        for iters in (200000, 100000):
            nh = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), iters).hex()
            if hmac.compare_digest(h, nh):
                return True
        return False
    except Exception:
        return False

def create_session_token(username: str) -> str:
    # itsdangerous уже ставит timestamp и HMAC-SHA512
    return _serializer.dumps({"u": username})

def verify_session_token(token: str) -> Optional[str]:
    try:
        data = _serializer.loads(token, max_age=SESSION_MAX_AGE)
        return data.get("u")
    except (BadSignature, SignatureExpired) as e:
        log.debug(f"session invalid: {e}")
        return None
    except Exception:
        return None

def role_hierarchy(role: str) -> int:
    order = {UserRole.observer.value: 0, UserRole.operator.value: 1, UserRole.admin.value: 2}
    return order.get(role, -1)

def can_access(required: str, actual: str) -> bool:
    return role_hierarchy(actual) >= role_hierarchy(required)

async def get_current_user(request: Request, db: AsyncSession = Depends(get_db)) -> Optional[User]:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    username = verify_session_token(token)
    if not username:
        return None
    res = await db.execute(select(User).where(User.username==username))
    user = res.scalar_one_or_none()
    if user and user.is_active:
        return user
    return None

async def require_login(request: Request, db: AsyncSession = Depends(get_db)) -> User:
    user = await get_current_user(request, db)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="login required")
    return user

def require_roles(*roles):
    async def checker(request: Request, db: AsyncSession = Depends(get_db)) -> User:
        user = await get_current_user(request, db)
        if not user:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="login required")
        if user.role == UserRole.admin.value:
            return user
        if user.role not in roles:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden: insufficient role")
        return user
    return checker

require_admin = require_roles(UserRole.admin.value)
require_operator = require_roles(UserRole.operator.value, UserRole.admin.value)
require_observer = require_roles(UserRole.observer.value, UserRole.operator.value, UserRole.admin.value)

async def ensure_default_admin():
    from .database import SessionLocal
    async with SessionLocal() as db:
        res = await db.execute(select(User).where(User.username==settings.admin_user))
        u = res.scalar_one_or_none()
        if not u:
            ph = hash_password(settings.admin_pass)
            admin = User(username=settings.admin_user, password_hash=ph, role=UserRole.admin.value)
            db.add(admin)
            await db.commit()
            log.info(f"Создан дефолтный админ {settings.admin_user}")
        else:
            if u.role != UserRole.admin.value:
                u.role = UserRole.admin.value
                await db.commit()
