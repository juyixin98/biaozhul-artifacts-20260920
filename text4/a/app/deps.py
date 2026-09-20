"""Authentication is intentionally dependency-free: the caller identifies
themselves with an ``X-User-Id`` header. This keeps the service self-contained
while still enforcing ownership and role checks at every endpoint.
"""
from __future__ import annotations

from fastapi import Depends, Header
from sqlalchemy.orm import Session

from .database import get_db
from .errors import AppError, forbidden
from .models import User, UserRole


def current_user(
    x_user_id: int | None = Header(default=None, alias="X-User-Id"),
    db: Session = Depends(get_db),
) -> User:
    if x_user_id is None:
        raise AppError(401, "UNAUTHENTICATED", "missing X-User-Id header")
    user = db.get(User, x_user_id)
    if user is None:
        raise AppError(401, "UNKNOWN_USER", "user referenced by X-User-Id does not exist")
    return user


def current_manager(user: User = Depends(current_user)) -> User:
    if user.role is not UserRole.MANAGER:
        raise forbidden("manager role required")
    return user


def current_learner(user: User = Depends(current_user)) -> User:
    if user.role is not UserRole.LEARNER:
        raise forbidden("learner role required")
    return user
