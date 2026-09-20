import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import AdminPrincipal, require_platform_admin
from app.errors import LeaseError
from app.models import AdminUser, Tenant
from app.schemas import AdminOut, TenantAdminCreate, TenantCreate, TenantOut
from app.security import hash_password

router = APIRouter(prefix="/api/platform", tags=["platform-admin"])


@router.post("/tenants", response_model=TenantOut, status_code=201)
def create_tenant(
    body: TenantCreate,
    db: Session = Depends(get_db),
    _: AdminPrincipal = Depends(require_platform_admin),
) -> Tenant:
    if db.execute(select(Tenant).where(Tenant.name == body.name)).scalar_one_or_none():
        raise LeaseError("tenant_name_taken", "tenant name already exists", status=409)
    tenant = Tenant(name=body.name)
    db.add(tenant)
    db.commit()
    db.refresh(tenant)
    return tenant


@router.get("/tenants", response_model=list[TenantOut])
def list_tenants(
    db: Session = Depends(get_db),
    _: AdminPrincipal = Depends(require_platform_admin),
) -> list[Tenant]:
    return list(db.execute(select(Tenant).order_by(Tenant.created_at)).scalars())


@router.post("/tenants/{tenant_id}/admins", response_model=AdminOut, status_code=201)
def create_tenant_admin(
    tenant_id: uuid.UUID,
    body: TenantAdminCreate,
    db: Session = Depends(get_db),
    _: AdminPrincipal = Depends(require_platform_admin),
) -> AdminUser:
    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        raise LeaseError("tenant_not_found", "tenant does not exist", status=404)
    if db.execute(select(AdminUser).where(AdminUser.username == body.username)).scalar_one_or_none():
        raise LeaseError("admin_username_taken", "username already exists", status=409)
    admin = AdminUser(
        username=body.username,
        password_hash=hash_password(body.password),
        tenant_id=tenant.id,
        is_platform_admin=False,
    )
    db.add(admin)
    db.commit()
    db.refresh(admin)
    return admin


@router.get("/tenants/{tenant_id}/admins", response_model=list[AdminOut])
def list_tenant_admins(
    tenant_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: AdminPrincipal = Depends(require_platform_admin),
) -> list[AdminUser]:
    if db.get(Tenant, tenant_id) is None:
        raise LeaseError("tenant_not_found", "tenant does not exist", status=404)
    return list(
        db.execute(select(AdminUser).where(AdminUser.tenant_id == tenant_id)).scalars()
    )
