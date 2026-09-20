from __future__ import annotations

from fastapi import APIRouter, Depends, Header, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..errors import ValidationError
from ..models import JournalEntry
from ..schemas import EntryIn, EntryOut, ReversalIn
from ..security import Principal, require_roles
from ..serializers import entry_to_out
from ..services import run_idempotent
from ..services.csv_upload import post_csv
from ..services.entries import create_entry, reverse_entry

router = APIRouter(prefix="/entries", tags=["entries"])

_WRITERS = require_roles("lead", "accountant")


@router.post("", response_model=EntryOut, status_code=201)
def post_entry(
    payload: EntryIn,
    db: Session = Depends(get_db),
    principal: Principal = Depends(_WRITERS),
    idempotency_key: str | None = Header(default=None, max_length=128),
):
    def handler():
        entry = create_entry(db, payload, actor=principal.name)
        return 201, entry_to_out(db, entry)

    status, body, _replayed = run_idempotent(db, idempotency_key, payload.model_dump(mode="json"), handler)
    return body


@router.post("/reversal", response_model=EntryOut, status_code=201)
def post_reversal(
    payload: ReversalIn,
    db: Session = Depends(get_db),
    principal: Principal = Depends(_WRITERS),
    idempotency_key: str | None = Header(default=None, max_length=128),
):
    def handler():
        entry = reverse_entry(db, payload, actor=principal.name)
        return 201, entry_to_out(db, entry)

    status, body, _replayed = run_idempotent(
        db, idempotency_key, payload.model_dump(mode="json"), handler
    )
    return body


@router.post("/csv", status_code=201)
async def post_entries_csv(
    file: UploadFile,
    db: Session = Depends(get_db),
    principal: Principal = Depends(_WRITERS),
    idempotency_key: str | None = Header(default=None, max_length=128),
):
    content = await file.read()
    if not content:
        raise ValidationError("Uploaded CSV is empty")

    def handler():
        entries = post_csv(db, content, actor=principal.name)
        return 201, {"posted_entries": [entry_to_out(db, e) for e in entries]}

    status, body, _replayed = run_idempotent(
        db,
        idempotency_key,
        {"filename": file.filename, "sha256": _sha(content)},
        handler,
    )
    return body


def _sha(content: bytes) -> str:
    import hashlib

    return hashlib.sha256(content).hexdigest()


@router.get("", response_model=list[EntryOut])
def list_entries(
    period_code: str | None = None,
    voucher_no: str | None = None,
    limit: int = 100,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_roles("lead", "accountant", "auditor")),
):
    limit = min(max(limit, 1), 1000)
    stmt = select(JournalEntry).order_by(JournalEntry.id.desc()).limit(limit)
    if period_code:
        stmt = (
            select(JournalEntry)
            .where(JournalEntry.period_code == period_code)
            .order_by(JournalEntry.id.desc())
            .limit(limit)
        )
    if voucher_no:
        stmt = select(JournalEntry).where(JournalEntry.voucher_no == voucher_no)
    rows = db.scalars(stmt).unique().all()
    return [entry_to_out(db, e) for e in rows]
