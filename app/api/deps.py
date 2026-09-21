"""Shared FastAPI dependencies."""

from __future__ import annotations

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import CareWorker, Coordinator
from app.services.assignments import AuthorizationError


def coordinator_id(x_coordinator_id: int | None = Header(default=None)) -> int | None:
    """Demo identity header. In production this would be a verified JWT claim."""
    return x_coordinator_id


def require_coordinator(
    coordinator_id: int | None = Depends(coordinator_id),
    db: Session = Depends(get_db),
) -> Coordinator:
    if coordinator_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="X-Coordinator-ID header is required",
        )
    coordinator = db.get(Coordinator, coordinator_id)
    if coordinator is None:
        raise HTTPException(status_code=404, detail="coordinator not found")
    return coordinator


def require_worker(
    x_worker_id: int | None = Header(default=None),
    db: Session = Depends(get_db),
) -> CareWorker:
    if x_worker_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="X-Worker-ID header is required",
        )
    worker = db.get(CareWorker, x_worker_id)
    if worker is None:
        raise HTTPException(status_code=404, detail="worker not found")
    return worker


def translate_authorization(callable_, *args, **kwargs):
    """Run a service call and map AuthorizationError to a 403 response."""
    try:
        return callable_(*args, **kwargs)
    except AuthorizationError as exc:  # pragma: no cover - thin wrapper
        raise HTTPException(status_code=403, detail=str(exc)) from exc
