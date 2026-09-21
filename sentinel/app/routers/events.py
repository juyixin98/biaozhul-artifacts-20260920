from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import UserRole
from ..schemas import BatchResponse, EventBatchIn
from ..security import require_roles
from ..detection.engine import ingest_batch

router = APIRouter(tags=["events"])


@router.post("/events/batch", response_model=BatchResponse)
def post_events(
    batch: EventBatchIn,
    db: Session = Depends(get_db),
    _=Depends(require_roles(UserRole.admin, UserRole.analyst)),
):
    """Ingest up to 2000 events atomically.

    - invalid item / unknown device / duplicate key within batch -> 400, nothing stored
    - same (device, event_id) with different content -> 409, nothing stored
    - identical retry -> counted as duplicates, not re-inserted; no duplicate alerts
    """
    result = ingest_batch(db, batch.events)
    db.commit()
    return result
