"""Platform-level bootstrap endpoints (organization provisioning).

Guarded by the management API key, independent of per-organization keys.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy.orm import Session

from . import services
from .db import get_db
from .errors import ConsentVaultError
from .security import constant_time_equals

router = APIRouter(prefix="/management", tags=["management"])


def require_management_key(
    request: Request, x_management_key: str | None = Header(default=None)
) -> None:
    settings = request.app.state.settings
    if not x_management_key or not constant_time_equals(
        x_management_key, settings.management_api_key
    ):
        err = ConsentVaultError("invalid management key")
        err.status_code = 401
        err.code = "unauthorized"
        raise err


@router.post("/organizations", response_model=dict, status_code=201,
             dependencies=[Depends(require_management_key)])
def create_organization(body: dict, db: Session = Depends(get_db)):
    name = str(body.get("name", "")).strip()
    if not name:
        raise ConsentVaultError("name is required")
    return services.create_organization(db, name)
