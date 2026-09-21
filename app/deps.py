"""Shared FastAPI dependencies: sessions, clock and identity/auth."""
from __future__ import annotations

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.clock import Clock
from app.config import Settings
from app.db import get_session
from app.models import Coordinator, Worker


def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings


def get_clock(request: Request) -> Clock:
    return request.app.state.clock


def require_api_key(
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    authorization: str | None = Header(default=None),
) -> None:
    configured = request.app.state.settings.api_key
    if not configured:
        return
    token = x_api_key
    if token is None and authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:]
    if token != configured:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid API key")


def current_coordinator(
    db: Session = Depends(get_session),
    coordinator_id: int | None = Header(default=None, alias="X-Coordinator-Id"),
) -> Coordinator:
    if coordinator_id is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="missing X-Coordinator-Id header")
    coordinator = db.get(Coordinator, coordinator_id)
    if coordinator is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unknown coordinator")
    return coordinator


def current_worker(
    db: Session = Depends(get_session),
    worker_id: int | None = Header(default=None, alias="X-Worker-Id"),
) -> Worker:
    if worker_id is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="missing X-Worker-Id header")
    worker = db.get(Worker, worker_id)
    if worker is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unknown worker")
    return worker


def assert_unit_authorized(coordinator: Coordinator, unit_id: int) -> None:
    """Coordinators may only schedule units they were granted (admins: all)."""
    if coordinator.is_admin:
        return
    granted = {g.unit_id for g in coordinator.unit_grants}
    if unit_id not in granted:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"coordinator {coordinator.id} is not authorized to schedule unit {unit_id}",
        )
