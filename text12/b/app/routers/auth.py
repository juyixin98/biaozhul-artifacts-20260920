from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import AdminPrincipal, get_current_admin
from app.models import AdminUser
from app.schemas import LoginRequest, TokenResponse
from app.security import create_access_token, verify_password

router = APIRouter(tags=["auth"])


@router.post("/api/admin/login", response_model=TokenResponse)
def admin_login(body: LoginRequest, db: Session = Depends(get_db)) -> TokenResponse:
    user = db.execute(
        select(AdminUser).where(AdminUser.username == body.username)
    ).scalar_one_or_none()
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="invalid username or password")
    token = create_access_token(
        user_id=str(user.id),
        tenant_id=str(user.tenant_id) if user.tenant_id else None,
        is_platform_admin=user.is_platform_admin,
    )
    return TokenResponse(
        access_token=token,
        tenant_id=user.tenant_id,
        is_platform_admin=user.is_platform_admin,
    )


@router.get("/api/admin/me")
def admin_me(admin: AdminPrincipal = Depends(get_current_admin)) -> dict:
    return {
        "id": str(admin.id),
        "username": admin.username,
        "tenant_id": str(admin.tenant_id) if admin.tenant_id else None,
        "is_platform_admin": admin.is_platform_admin,
    }
