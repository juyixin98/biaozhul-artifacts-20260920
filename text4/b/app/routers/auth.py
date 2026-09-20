"""Authentication endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.deps import current_user, get_db
from app.models import User
from app.schemas.dto import LoginRequest, RegisterRequest, TokenResponse, UserResponse
from app.services import auth_service

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/register", response_model=TokenResponse, status_code=201)
def register(payload: RegisterRequest, session: Session = Depends(get_db)) -> TokenResponse:
    user = auth_service.register(
        session,
        name=payload.name,
        login=payload.login,
        password=payload.password,
        role=payload.role,
    )
    session.commit()
    token = auth_service.new_token_for(user, session)
    return TokenResponse(token=token, user=UserResponse.model_validate(user))


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest, session: Session = Depends(get_db)) -> TokenResponse:
    user, token = auth_service.login(
        session, login=payload.login, password=payload.password
    )
    return TokenResponse(token=token, user=UserResponse.model_validate(user))


@router.get("/me", response_model=UserResponse)
def me(user: User = Depends(current_user)) -> UserResponse:
    return UserResponse.model_validate(user)
