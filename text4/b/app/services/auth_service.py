"""Authentication: user registration, login and token sessions."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.errors import AuthError, ConflictError
from app.models import AuthSession, User
from app.security import hash_password, new_token, verify_password


def register(session: Session, *, name: str, login: str, password: str, role: str) -> User:
    user = User(name=name, login=login, password_hash=hash_password(password), role=role)
    session.add(user)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        raise ConflictError(f"login '{login}' is already registered")
    return user


def new_token_for(user: User, session: Session) -> str:
    token = new_token()
    session.add(AuthSession(token=token, user_id=user.id))
    session.commit()
    return token


def login(session: Session, *, login: str, password: str) -> tuple[User, str]:
    user = session.scalar(select(User).where(User.login == login))
    if user is None or not verify_password(password, user.password_hash):
        raise AuthError("invalid login or password", status_code=401)
    token = new_token()
    session.add(AuthSession(token=token, user_id=user.id))
    session.commit()
    return user, token


def user_for_token(session: Session, token: str) -> User:
    auth_session = session.get(AuthSession, token)
    if auth_session is None:
        raise AuthError("missing or invalid token", status_code=401)
    user = session.get(User, auth_session.user_id)
    if user is None:  # pragma: no cover - defensive
        raise AuthError("user no longer exists", status_code=401)
    return user
