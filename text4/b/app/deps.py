"""Shared FastAPI dependencies: DB session and authenticated users."""
from __future__ import annotations

from typing import Iterator

from fastapi import Depends, Header
from sqlalchemy.orm import Session

from app.database import get_session
from app.errors import AuthError, ForbiddenError
from app.models import User
from app.services.auth_service import user_for_token


def get_db() -> Iterator[Session]:
    yield from get_session()


def current_user(
    authorization: str | None = Header(default=None),
    session: Session = Depends(get_db),
) -> User:
    if not authorization:
        raise AuthError("missing Authorization header", status_code=401)
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise AuthError("expected 'Authorization: Bearer <token>'", status_code=401)
    return user_for_token(session, token)


def require_supervisor(user: User = Depends(current_user)) -> User:
    if user.role != "supervisor":
        raise ForbiddenError("supervisor role required")
    return user


def require_learner(user: User = Depends(current_user)) -> User:
    if user.role != "learner":
        raise ForbiddenError("learner role required")
    return user
