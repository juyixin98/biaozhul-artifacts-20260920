import csv
import io
from decimal import Decimal, InvalidOperation
from typing import Annotated

from fastapi import APIRouter, Depends, File, Header, Response, UploadFile
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..db import get_db
from ..errors import DomainError
from ..models import Account, Department, FiscalPeriod, Fund, JournalEntry
from ..schemas import (
    MAX_CENTS,
    JournalCreate,
    JournalEntryOut,
    JournalLineIn,
    ReversalCreate,
)
from ..security import CurrentUser, require_role
from ..services.journals import payload_digest, post_journal, reverse_journal

router = APIRouter(prefix="/journals", tags=["journals"])

Db = Annotated[Session, Depends(get_db)]
Clerk = Annotated[CurrentUser, Depends(require_role("clerk", "finance_manager"))]
Reader = Annotated[CurrentUser, Depends(require_role("clerk", "approver", "finance_manager", "auditor"))]

MAX_CSV_ROWS = 2000
MAX_CSV_BYTES = 4 * 1024 * 1024


@router.post("", response_model=JournalEntryOut, status_code=201)
def create_journal(payload: JournalCreate, response: Response, db: Db, user: Clerk):
    try:
        entry, replayed = post_journal(db, payload, user.username)
        db.commit()
    except IntegrityError:
        # Lost a race on the idempotency-key unique constraint: the same key
        # was inserted concurrently. Resolve deterministically.
        db.rollback()
        existing = db.scalar(
            select(JournalEntry).where(
                JournalEntry.idempotency_key == payload.idempotency_key
            )
        )
        if existing is not None and existing.payload_hash == payload_digest(payload):
            response.status_code = 200
            return existing
        raise DomainError(
            409,
            "idempotency-key-conflict",
            "idempotency key was already used with a different payload",
        )
    if replayed:
        response.status_code = 200
    return entry


@router.post("/import", status_code=201)
def import_journals_csv(
    year: int,
    period: int,
    db: Db,
    user: Clerk,
    file: UploadFile = File(...),
    x_idempotency_key: str = Header(min_length=1, max_length=100),
):
    """Bulk-import a CSV of vouchers. All-or-nothing: any error rolls the
    whole batch back. Max 2000 data rows.

    CSV columns: voucher_no,fund_code,department_code,account_code,debit,credit
    (debit/credit in currency units with at most 2 decimals; exactly one per row).
    """
    period_obj = db.scalar(
        select(FiscalPeriod).where(
            FiscalPeriod.year == year, FiscalPeriod.period == period
        )
    )
    if period_obj is None:
        raise DomainError(404, "period-not-found", f"no period {year}-{period:02d}")

    raw = file.file.read(MAX_CSV_BYTES + 1)
    if len(raw) > MAX_CSV_BYTES:
        raise DomainError(422, "csv-too-large", "file exceeds 4 MiB")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise DomainError(422, "csv-invalid-encoding", "file must be UTF-8")

    reader = csv.DictReader(io.StringIO(text))
    required = {"voucher_no", "fund_code", "department_code", "account_code", "debit", "credit"}
    if not reader.fieldnames or not required.issubset({f.strip() for f in reader.fieldnames}):
        raise DomainError(
            422, "csv-bad-header", f"required columns: {sorted(required)}"
        )
    rows = list(reader)
    if not rows:
        raise DomainError(422, "csv-empty", "no data rows")
    if len(rows) > MAX_CSV_ROWS:
        raise DomainError(
            422, "csv-too-many-rows", f"{len(rows)} rows exceeds limit of {MAX_CSV_ROWS}"
        )

    funds = {f.code: f.id for f in db.scalars(select(Fund))}
    depts = {d.code: d.id for d in db.scalars(select(Department))}
    accts = {a.code: a.id for a in db.scalars(select(Account))}

    def parse_amount(value, row_no, field, errors):
        value = (value or "").strip()
        if not value:
            return 0
        try:
            d = Decimal(value)
        except InvalidOperation:
            errors.append(f"row {row_no}: {field} {value!r} is not a number")
            return 0
        if d < 0 or d.as_tuple().exponent < -2 or d * 100 > MAX_CENTS:
            errors.append(f"row {row_no}: {field} {value!r} out of range (>= 0, max 2 decimals)")
            return 0
        return int(d * 100)

    # Group rows by voucher, preserving file order.
    groups: dict[str, list[tuple[int, dict]]] = {}
    errors: list[str] = []
    for row_no, row in enumerate(rows, start=2):  # header is line 1
        voucher = (row.get("voucher_no") or "").strip()
        if not voucher:
            errors.append(f"row {row_no}: missing voucher_no")
            continue
        groups.setdefault(voucher, []).append((row_no, row))

    payloads: list[tuple[str, JournalCreate]] = []
    for voucher, items in groups.items():
        lines: list[JournalLineIn] = []
        for row_no, row in items:
            fund_code = (row.get("fund_code") or "").strip()
            dept_code = (row.get("department_code") or "").strip()
            acct_code = (row.get("account_code") or "").strip()
            fid, did, aid = funds.get(fund_code), depts.get(dept_code), accts.get(acct_code)
            if fid is None:
                errors.append(f"row {row_no}: unknown fund_code {fund_code!r}")
            if did is None:
                errors.append(f"row {row_no}: unknown department_code {dept_code!r}")
            if aid is None:
                errors.append(f"row {row_no}: unknown account_code {acct_code!r}")
            debit = parse_amount(row.get("debit"), row_no, "debit", errors)
            credit = parse_amount(row.get("credit"), row_no, "credit", errors)
            if None in (fid, did, aid):
                continue
            if (debit > 0) == (credit > 0):
                errors.append(f"row {row_no}: exactly one of debit/credit must be positive")
                continue
            lines.append(
                JournalLineIn(
                    fund_id=fid, department_id=did, account_id=aid,
                    debit_cents=debit, credit_cents=credit,
                )
            )
        if not lines:
            continue
        total_debit = sum(l.debit_cents for l in lines)
        total_credit = sum(l.credit_cents for l in lines)
        if total_debit == 0 or total_debit != total_credit:
            errors.append(
                f"voucher {voucher!r}: unbalanced (debits {total_debit} != credits {total_credit})"
            )
            continue
        payloads.append(
            (
                voucher,
                JournalCreate(
                    idempotency_key=f"{x_idempotency_key}:{voucher}",
                    period_id=period_obj.id,
                    memo=f"csv voucher {voucher}",
                    lines=lines,
                ),
            )
        )

    if errors:
        # Nothing has been written yet; the whole batch is rejected.
        raise DomainError(422, "csv-validation-failed", errors)

    results = []
    for voucher, payload in payloads:
        entry, replayed = post_journal(db, payload, user.username, source="csv")
        results.append({"voucher_no": voucher, "entry_id": entry.id, "replayed": replayed})
    db.commit()
    return {"imported": len(results), "entries": results}


@router.post("/{entry_id}/reverse", response_model=JournalEntryOut, status_code=201)
def reverse_entry(entry_id: int, payload: ReversalCreate, response: Response, db: Db, user: Clerk):
    try:
        entry, replayed = reverse_journal(db, entry_id, payload, user.username)
        db.commit()
    except IntegrityError:
        db.rollback()
        if db.scalar(
            select(JournalEntry).where(JournalEntry.reversal_of_id == entry_id)
        ) is not None:
            raise DomainError(409, "already-reversed", f"entry {entry_id} was already reversed")
        raise DomainError(409, "integrity-error", "concurrent write conflict; retry")
    if replayed:
        response.status_code = 200
    return entry


@router.get("", response_model=list[JournalEntryOut])
def list_journals(db: Db, user: Reader, period_id: int | None = None, limit: int = 100):
    stmt = select(JournalEntry).order_by(JournalEntry.id).limit(min(limit, 500))
    if period_id is not None:
        stmt = select(JournalEntry).where(JournalEntry.period_id == period_id).order_by(JournalEntry.id).limit(min(limit, 500))
    return db.scalars(stmt).all()


@router.get("/{entry_id}", response_model=JournalEntryOut)
def get_journal(entry_id: int, db: Db, user: Reader):
    entry = db.get(JournalEntry, entry_id)
    if entry is None:
        raise DomainError(404, "entry-not-found", f"journal entry {entry_id} does not exist")
    return entry
