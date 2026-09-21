from __future__ import annotations

import hashlib
import hmac
import secrets

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.errors import Unauthorized
from app.models import User

API_KEY_PREFIX = "vcak_"
API_KEY_BYTES = 32


def generate_api_key() -> str:
    """New plaintext key, shown to the caller exactly once."""
    return API_KEY_PREFIX + secrets.token_urlsafe(API_KEY_BYTES)


def hash_api_key(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def get_current_user(
    request: Request, db: Session = Depends(get_db)
) -> User:
    header = request.headers.get("authorization", "")
    if not header.startswith("Bearer "):
        raise Unauthorized("missing_api_key", "Authorization: Bearer <api-key> required")
    token = header[len("Bearer "):].strip()
    if not token.startswith(API_KEY_PREFIX) or len(token) < 32:
        raise Unauthorized("invalid_api_key", "invalid API key")
    digest = hash_api_key(token)
    user = db.scalar(select(User).where(User.api_key_hash == digest))
    if user is None:
        # constant-time-ish: still compare against nothing; key shape was checked.
        hmac.compare_digest(digest, digest)
        raise Unauthorized("invalid_api_key", "invalid API key")
    request.state.user_id = user.id
    return user
