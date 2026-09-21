from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import User
from ..schemas import LoginRequest, TokenResponse, UserOut
from ..security import create_access_token, get_current_user, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])


def _authenticate(db: Session, username: str, password: str) -> User:
    user = db.scalars(select(User).where(User.username == username)).first()
    if user is None or not user.is_active or not verify_password(password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


@router.post("/token", response_model=TokenResponse)
def login_form(form: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    """OAuth2 password-grant endpoint (form-encoded), for Swagger UI."""
    user = _authenticate(db, form.username, form.password)
    token, expires_in = create_access_token(user)
    return TokenResponse(
        access_token=token, expires_in=expires_in, role=user.role, username=user.username
    )


@router.post("/login", response_model=TokenResponse)
def login_json(body: LoginRequest, db: Session = Depends(get_db)):
    """JSON login endpoint."""
    user = _authenticate(db, body.username, body.password)
    token, expires_in = create_access_token(user)
    return TokenResponse(
        access_token=token, expires_in=expires_in, role=user.role, username=user.username
    )


@router.get("/me", response_model=UserOut)
def me(current: User = Depends(get_current_user)):
    return current
