from fastapi import APIRouter, Body, Depends, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..errors import DomainError
from ..models import JournalEntry, Period
from ..schemas import JournalEntryCreate, ReverseCreate
from ..security import ACCOUNTANT, FINANCE_OFFICER, User, get_current_user, require_roles
from ..services import journals as journal_service

router = APIRouter(prefix="/journals", tags=["journals"])

WRITE = require_roles(ACCOUNTANT, FINANCE_OFFICER)


@router.post("/import", status_code=201)
def import_journals(
    year: int,
    month: int,
    csv_text: str = Body(media_type="text/plain"),
    db: Session = Depends(get_db),
    user: User = Depends(WRITE),
):
    return journal_service.import_csv(db, csv_text, year, month, user.id)


@router.post("", status_code=201)
def post(
    payload: JournalEntryCreate,
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(WRITE),
):
    entry, created = journal_service.post_journal(db, payload, user.id)
    if not created:
        # Idempotent replay: same key + same content -> original result.
        response.status_code = 200
    return journal_service.entry_dict(db, entry)


@router.get("")
def list_journals(
    year: int,
    month: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    entries = db.scalars(
        select(JournalEntry)
        .join(Period, JournalEntry.period_id == Period.id)
        .where(Period.year == year, Period.month == month)
        .order_by(JournalEntry.id)
    ).all()
    return [journal_service.entry_dict(db, e) for e in entries]


@router.get("/{entry_id}")
def get_journal(
    entry_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    entry = db.get(JournalEntry, entry_id)
    if entry is None:
        raise DomainError(404, f"journal entry {entry_id} not found")
    return journal_service.entry_dict(db, entry)


@router.post("/{entry_id}/reverse", status_code=201)
def reverse(
    entry_id: int,
    payload: ReverseCreate,
    db: Session = Depends(get_db),
    user: User = Depends(WRITE),
):
    entry = journal_service.reverse_journal(
        db, entry_id, payload.year, payload.month, user.id, payload.memo
    )
    return journal_service.entry_dict(db, entry)
