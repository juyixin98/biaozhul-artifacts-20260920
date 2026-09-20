"""Instance endpoints: start, inspect position and read history."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..engine import start_instance
from ..models import HistoryRecord, Instance
from ..schemas import InstanceStart
from ..serializers import serialize_history, serialize_instance

router = APIRouter(tags=["instances"])


def _get_or_404(db: Session, instance_id: uuid.UUID) -> Instance:
    instance = db.scalar(select(Instance).where(Instance.id == instance_id))
    if instance is None:
        from ..errors import DomainError

        raise DomainError("instance_not_found", f"instance {instance_id} not found", 404)
    return instance


@router.post("/instances", status_code=201)
def create_instance(body: InstanceStart, db: Session = Depends(get_db)):
    instance = start_instance(db, body)
    return serialize_instance(db, instance)


@router.get("/instances/{instance_id}")
def get_instance(instance_id: uuid.UUID, db: Session = Depends(get_db)):
    """Current position, open todos, status and rejection reason."""
    return serialize_instance(db, _get_or_404(db, instance_id))


@router.get("/instances/{instance_id}/history")
def get_history(instance_id: uuid.UUID, db: Session = Depends(get_db)):
    instance = _get_or_404(db, instance_id)
    records = list(
        db.scalars(
            select(HistoryRecord)
            .where(HistoryRecord.instance_id == instance.id)
            .order_by(HistoryRecord.seq)
        ).all()
    )
    return {
        "instance_id": instance.id,
        "version": instance.version_number,
        "status": instance.status,
        "history": serialize_history(records),
    }
