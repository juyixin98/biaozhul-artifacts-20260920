"""Decision and withdraw endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..db import get_db
from ..engine import decide, withdraw
from ..schemas import DecisionIn, WithdrawIn

router = APIRouter(tags=["approvals"])


@router.post("/decisions")
def post_decision(body: DecisionIn, db: Session = Depends(get_db)):
    """Approve or reject an open todo.

    Idempotent via ``request_id``: a repeated call returns the first call's
    stored result (with ``replayed=true``); a repeat whose other fields
    disagree returns 409 and writes no history.
    """
    try:
        result, _ = decide(db, body)
    except IntegrityError:
        db.rollback()
        # Concurrent insert of the same request_id: read the winner and replay
        # it (or 409 if it disagrees) via the normal code path.
        result, _ = decide(db, body)
    return result


@router.post("/withdrawals")
def post_withdraw(body: WithdrawIn, db: Session = Depends(get_db)):
    """Submitter withdraws a flow before it ends; idempotent via request_id."""
    try:
        result, _ = withdraw(db, body)
    except IntegrityError:
        db.rollback()
        result, _ = withdraw(db, body)
    return result
