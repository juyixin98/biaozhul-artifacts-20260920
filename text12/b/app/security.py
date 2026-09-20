import datetime as dt
import hashlib
import hmac
import secrets

import bcrypt
import jwt

from app.config import settings


# ---------------------------------------------------------------------------
# Passwords (admin login)
# ---------------------------------------------------------------------------

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Opaque tokens (device credential + per-lease secret)
# ---------------------------------------------------------------------------

def generate_token() -> str:
    """A fresh opaque credential shown to the caller exactly once."""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def compare_token_hash(token: str, token_hash: str) -> bool:
    return hmac.compare_digest(hash_token(token), token_hash)


# ---------------------------------------------------------------------------
# Admin JWT
# ---------------------------------------------------------------------------

def create_access_token(*, user_id: str, tenant_id: str | None, is_platform_admin: bool) -> str:
    now = dt.datetime.now(dt.timezone.utc)
    payload = {
        "sub": user_id,
        "tid": tenant_id,
        "pa": is_platform_admin,
        "iat": int(now.timestamp()),
        "exp": int((now + dt.timedelta(minutes=settings.jwt_expire_minutes)).timestamp()),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict:
    return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
