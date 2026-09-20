from fastapi import APIRouter, Depends, Header, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import JournalEntry
from app.schemas import (
    ImportResult,
    JournalCreateIn,
    JournalEntryOut,
    PostResult,
)
from app.security import Identity, require_write, get_identity
from app.services import importer, posting

router = APIRouter(prefix="/journals", tags=["journals"])


@router.post("", response_model=PostResult, status_code=201)
def post_journal(
    body: JournalCreateIn,
    db: Session = Depends(get_db),
    identity: Identity = Depends(require_write),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    """过账一张凭证。必须携带 Idempotency-Key。"""
    if not idempotency_key:
        raise HTTPException(status_code=422, detail={"code": "MISSING_IDEMPOTENCY_KEY",
                                                     "message": "缺少 Idempotency-Key 请求头"})
    lines = [l.model_dump() for l in body.lines]
    entry, replayed = posting.post_journal(
        db,
        period_id=body.period_id,
        description=body.description,
        lines=lines,
        actor=identity.name,
        idempotency_key=idempotency_key,
    )
    return PostResult(entry=JournalEntryOut.model_validate(entry),
                      idempotent_replay=replayed)


@router.post("/import", response_model=ImportResult, status_code=201)
def import_journals(
    file: UploadFile,
    db: Session = Depends(get_db),
    identity: Identity = Depends(require_write),
):
    """CSV 批量导入：最多 2000 行，按凭证号归组，任何错误整批回滚。"""
    content = file.file.read()
    entry_ids = importer.import_csv(db, content=content, actor=identity.name)
    return ImportResult(vouchers=len(entry_ids), entry_ids=entry_ids)


@router.post("/{entry_id}/reverse", response_model=JournalEntryOut, status_code=201)
def reverse_journal(
    entry_id: int,
    period_id: int,
    reason: str,
    db: Session = Depends(get_db),
    identity: Identity = Depends(require_write),
):
    """在开放期间对已过账凭证追加冲销凭证（已过账凭证本身不可改）。"""
    if not reason or len(reason.strip()) < 5:
        raise HTTPException(status_code=422, detail={"code": "REASON_REQUIRED",
                                                     "message": "冲销必须说明原因（至少 5 个字符）"})
    entry = posting.reverse_journal(
        db, entry_id=entry_id, period_id=period_id,
        actor=identity.name, reason=reason.strip(),
    )
    return JournalEntryOut.model_validate(entry)


@router.get("", response_model=list[JournalEntryOut])
def list_journals(
    period_id: int | None = None,
    limit: int = 100,
    offset: int = 0,
    db: Session = Depends(get_db),
    _: Identity = Depends(get_identity),
):
    stmt = select(JournalEntry).order_by(JournalEntry.id).limit(min(limit, 500)).offset(offset)
    if period_id is not None:
        stmt = stmt.where(JournalEntry.period_id == period_id)
    return [JournalEntryOut.model_validate(e) for e in db.scalars(stmt)]


@router.get("/{entry_id}", response_model=JournalEntryOut)
def get_journal(
    entry_id: int,
    db: Session = Depends(get_db),
    _: Identity = Depends(get_identity),
):
    entry = db.get(JournalEntry, entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail={"code": "ENTRY_NOT_FOUND",
                                                     "message": f"凭证 {entry_id} 不存在"})
    return JournalEntryOut.model_validate(entry)
