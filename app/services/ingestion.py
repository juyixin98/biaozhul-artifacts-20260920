"""Batch event ingestion with idempotency and conflict detection.

Guarantees:
- The whole batch is one transaction: any invalid item or content conflict
  rolls everything back.
- (device_id, event_id) is unique; identical retransmits are skipped as
  duplicates, even under concurrency (INSERT .. ON CONFLICT DO NOTHING).
- Same id with different content raises BatchConflictError -> HTTP 409.
"""
import hashlib
import json

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models import Device, Event
from app.schemas import EventIn
from app.services.detection import run_detection


class BatchValidationError(Exception):
    pass


class BatchConflictError(Exception):
    def __init__(self, conflicts: list[dict]):
        super().__init__("event content conflict")
        self.conflicts = conflicts


def content_hash(item: EventIn) -> str:
    canon = json.dumps(
        {
            "event_type": item.event_type,
            "occurred_at": item.occurred_at.isoformat(),
            "payload": item.payload,
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def ingest_batch(db: Session, items: list[EventIn]) -> dict:
    device_ids = sorted({i.device_id for i in items})
    devices = db.query(Device).filter(Device.id.in_(device_ids)).all()
    dmap = {d.id: d for d in devices}
    missing = [d for d in device_ids if d not in dmap]
    if missing:
        raise BatchValidationError(f"unknown device_id(s): {missing}")

    inserted_ids: list[int] = []
    duplicates = 0
    conflicts: list[dict] = []

    for item in items:
        dev = dmap[item.device_id]
        chash = content_hash(item)
        stmt = (
            pg_insert(Event)
            .values(
                device_id=dev.id,
                employee_id=dev.employee_id,
                event_id=item.event_id,
                event_type=item.event_type,
                occurred_at=item.occurred_at,
                payload=item.payload,
                content_hash=chash,
            )
            .on_conflict_do_nothing(constraint="uq_event_device_event")
            .returning(Event.id)
        )
        new_id = db.execute(stmt).scalar()
        if new_id is not None:
            inserted_ids.append(new_id)
            continue
        existing = (
            db.query(Event)
            .filter(Event.device_id == item.device_id, Event.event_id == item.event_id)
            .one()
        )
        if existing.content_hash != chash:
            conflicts.append({"device_id": item.device_id, "event_id": item.event_id})
        else:
            duplicates += 1

    if conflicts:
        raise BatchConflictError(conflicts)

    events = db.query(Event).filter(Event.id.in_(inserted_ids)).all() if inserted_ids else []
    alerts_created = run_detection(db, events)
    return {
        "inserted": len(inserted_ids),
        "duplicates": duplicates,
        "alerts_created": alerts_created,
    }
