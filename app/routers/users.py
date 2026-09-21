from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app import audit
from app.config import settings
from app.db import get_db
from app.errors import Forbidden
from app.models import User
from app.schemas import UserCreate, UserOut
from app.security import generate_api_key, hash_api_key
import uuid

router = APIRouter(prefix="/users", tags=["users"])


@router.post("", response_model=UserOut, status_code=201)
def register(
    body: UserCreate, request: Request, db: Session = Depends(get_db)
) -> UserOut:
    """Dev-only self-service registration. Disabled unless
    VAULT_ALLOW_REGISTRATION=true (default in local development)."""
    if not settings.allow_registration:
        raise Forbidden(
            "registration_disabled",
            "self-service registration is disabled; ask an operator for a key",
        )
    plaintext = generate_api_key()
    user = User(
        id=str(uuid.uuid4()), name=body.name, api_key_hash=hash_api_key(plaintext)
    )
    db.add(user)
    db.flush()
    audit.record(
        db,
        user_id=user.id,
        action="user.register",
        result=audit.OK,
        request_id=getattr(request.state, "request_id", None),
        detail="self-service API key issuance",
    )
    db.commit()
    db.refresh(user)
    out = UserOut.model_validate(user)
    out.api_key = plaintext  # shown once and only here
    return out
