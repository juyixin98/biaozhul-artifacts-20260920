"""Candidate selection wrapper used by the allocation service."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Task, Worker
from app.services.constraints import RankedCandidate, rank_candidates


def choose_candidate(
    session: Session, task: Task, *, exclude: set[int] | None = None
) -> list[RankedCandidate]:
    # Include inactive workers so their specific violation is reported; the
    # feasibility filter guarantees they are never invited.
    workers = list(session.scalars(select(Worker).order_by(Worker.id)))
    return rank_candidates(session, task, workers, exclude_workers=exclude)
