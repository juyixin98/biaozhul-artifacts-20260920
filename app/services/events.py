"""Event log: gapless per-job sequence numbers used for SSE replay."""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models.orm import Event, Job


def append_event(
    db: Session, job_id: str, kind: str, payload: dict, seq: int | None = None
) -> Event:
    if seq is None:
        # Serialised inside the worker's single-writer transaction; the
        # unique(job_id, seq) constraint remains the hard guarantee.
        current = db.scalar(
            select(func.coalesce(func.max(Event.seq), 0)).where(
                Event.job_id == job_id
            )
        )
        seq = int(current) + 1  # type: ignore[arg-type]
    row = Event(job_id=job_id, seq=seq, kind=kind, payload_json=payload)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def read_events(db: Session, job_id: str, after_seq: int = 0) -> list[Event]:
    return list(
        db.scalars(
            select(Event)
            .where(Event.job_id == job_id, Event.seq > after_seq)
            .order_by(Event.seq)
        )
    )
