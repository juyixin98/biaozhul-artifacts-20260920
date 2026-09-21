from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_user
from app.models import User
from app.schemas import BatchIn, BatchResult
from app.services.ingestion import (
    BatchConflictError,
    BatchValidationError,
    ingest_batch,
)

router = APIRouter(prefix="/api/events", tags=["events"])


@router.post("/batch", response_model=BatchResult)
def ingest(
    batch: BatchIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        result = ingest_batch(db, batch.events)
        db.commit()
        return result
    except BatchValidationError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc))
    except BatchConflictError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail={"message": "event content conflict", "conflicts": exc.conflicts},
        )
