"""Minimal authentication for a demo backend.

Callers identify themselves with ``X-User-Id: <id>``. The user's role is
loaded from the database and checked per route. This deliberately models an
internal/trusted edge (no external services); swap for a real identity
provider in production without touching the services layer.
"""
from dataclasses import dataclass

from fastapi import Depends, Header
from sqlalchemy.orm import Session

from app.database import get_db
from app.errors import AuthError, ForbiddenError
from app.models import User, ROLE_LEARNER, ROLE_SUPERVISOR


@dataclass
class CurrentUser:
    id: int
    role: str
    name: str


def get_current_user(
    x_user_id: int | None = Header(default=None, alias="X-User-Id"),
    db: Session = Depends(get_db),
) -> CurrentUser:
    if x_user_id is None:
        raise AuthError("Missing X-User-Id header.")
    user = db.get(User, x_user_id)
    if user is None:
        raise AuthError(f"User {x_user_id} does not exist.")
    return CurrentUser(id=user.id, role=user.role, name=user.name)


def require_supervisor(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    if user.role != ROLE_SUPERVISOR:
        raise ForbiddenError("Supervisor role required.")
    return user


def require_learner(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    if user.role != ROLE_LEARNER:
        raise ForbiddenError("Learner role required.")
    return user
