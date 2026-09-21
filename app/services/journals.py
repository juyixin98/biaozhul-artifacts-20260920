"""Journal posting, reversal and CSV import.

Concurrency notes:
- Posting and period close serialize on the period row: both sides run an
  atomic guarded UPDATE on it first, so exactly one ordering wins and a
  posting can never land in a period that is already closed.
- Idempotent posting: the idempotency key is unique. A retry with identical
  content returns the original entry; the same key with different content is
  a 409 conflict. The unique-constraint race (two concurrent first posts with
  the same key) is resolved via a SAVEPOINT and a re-read.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import uuid

from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..errors import DomainError
from ..models import (
    Account,
    Budget,
    Department,
    ExpenseRequest,
    Fund,
    JournalEntry,
    JournalLine,
    Period,
)
from ..schemas import JournalEntryCreate, JournalLineIn

MAX_CSV_ROWS = 2000
REQUIRED_CSV_COLUMNS = {
    "voucher_no",
    "fund_code",
    "department_code",
    "account_code",
    "debit_cents",
    "credit_cents",
}


def lock_open_period(db: Session, year: int, month: int) -> Period:
    """Verify the period is open and write-lock its row until commit.

    The guarded UPDATE both checks the status and takes the row lock, so a
    concurrent close either commits first (this raises 409) or blocks until
    this transaction commits (the close then proceeds). Explicit ordering.
    """
    period = db.scalar(select(Period).where(Period.year == year, Period.month == month))
    if period is None:
        raise DomainError(404, f"period {year}-{month:02d} does not exist")
    res = db.execute(
        update(Period)
        .where(Period.id == period.id, Period.status == "open")
        .values(lock_version=Period.lock_version + 1)
    )
    if res.rowcount != 1:
        raise DomainError(409, f"period {year}-{month:02d} is closed")
    return period


def _content_hash(payload: JournalEntryCreate) -> str:
    data = payload.model_dump()
    data.pop("idempotency_key", None)
    data["lines"] = sorted(
        data["lines"],
        key=lambda l: (
            l["fund_code"],
            l["department_code"],
            l["account_code"],
            l["debit_cents"],
            l["credit_cents"],
            l["memo"],
        ),
    )
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def _reference_maps(db: Session, payload: JournalEntryCreate):
    def resolve(model, label, codes):
        rows = {r.code: r for r in db.scalars(select(model).where(model.code.in_(codes)))}
        missing = codes - rows.keys()
        if missing:
            raise DomainError(422, f"unknown {label} codes: {sorted(missing)}")
        inactive = sorted(c for c, r in rows.items() if not r.active)
        if inactive:
            raise DomainError(422, f"inactive {label} codes: {inactive}")
        return {c: r.id for c, r in rows.items()}

    funds = resolve(Fund, "fund", {l.fund_code for l in payload.lines})
    departments = resolve(Department, "department", {l.department_code for l in payload.lines})
    accounts = resolve(Account, "account", {l.account_code for l in payload.lines})
    return funds, departments, accounts


def _settle_expense_request(db: Session, request_id: int, entry_total_cents: int) -> None:
    """Release the encumbrance and record the actual spend, exactly once."""
    req = db.get(ExpenseRequest, request_id)
    if req is None:
        raise DomainError(404, f"expense request {request_id} not found")
    if req.amount_cents != entry_total_cents:
        raise DomainError(
            422,
            f"entry total {entry_total_cents} does not match expense request "
            f"amount {req.amount_cents}",
        )
    res = db.execute(
        update(ExpenseRequest)
        .where(ExpenseRequest.id == request_id, ExpenseRequest.status == "approved")
        .values(status="posted")
    )
    if res.rowcount != 1:
        raise DomainError(
            409, f"expense request {request_id} is not in 'approved' state (is {req.status!r})"
        )
    res = db.execute(
        update(Budget)
        .where(Budget.id == req.budget_id, Budget.encumbered_cents >= req.amount_cents)
        .values(
            encumbered_cents=Budget.encumbered_cents - req.amount_cents,
            actual_cents=Budget.actual_cents + req.amount_cents,
        )
    )
    if res.rowcount != 1:
        raise DomainError(409, "encumbered balance is insufficient to settle this request")


def _post_impl(db: Session, payload: JournalEntryCreate, actor: str) -> tuple[JournalEntry, bool]:
    """Post one entry inside the caller's transaction. Returns (entry, created)."""
    content_hash = _content_hash(payload)
    existing = db.scalar(
        select(JournalEntry).where(JournalEntry.idempotency_key == payload.idempotency_key)
    )
    if existing is not None:
        if existing.content_hash == content_hash:
            return existing, False
        raise DomainError(409, "idempotency key was already used with a different payload")

    period = lock_open_period(db, payload.year, payload.month)
    funds, departments, accounts = _reference_maps(db, payload)

    total_debit = sum(l.debit_cents for l in payload.lines)
    total_credit = sum(l.credit_cents for l in payload.lines)
    if total_debit == 0 or total_debit != total_credit:
        raise DomainError(
            422, f"entry is not balanced: debits {total_debit} != credits {total_credit}"
        )

    voucher_no = payload.voucher_no or f"V-{uuid.uuid4().hex[:12].upper()}"
    entry = JournalEntry(
        voucher_no=voucher_no,
        period_id=period.id,
        idempotency_key=payload.idempotency_key,
        content_hash=content_hash,
        memo=payload.memo,
        expense_request_id=payload.expense_request_id,
        created_by=actor,
    )
    try:
        with db.begin_nested():
            db.add(entry)
            db.flush()
            for line in payload.lines:
                db.add(
                    JournalLine(
                        entry_id=entry.id,
                        fund_id=funds[line.fund_code],
                        department_id=departments[line.department_code],
                        account_id=accounts[line.account_code],
                        debit_cents=line.debit_cents,
                        credit_cents=line.credit_cents,
                        memo=line.memo,
                    )
                )
            db.flush()
    except IntegrityError:
        # Lost a concurrent race on the idempotency key or voucher number.
        again = db.scalar(
            select(JournalEntry).where(JournalEntry.idempotency_key == payload.idempotency_key)
        )
        if again is not None and again.content_hash == content_hash:
            return again, False
        raise DomainError(409, "duplicate voucher number or idempotency key")

    # Settle only after the entry insert succeeded, so an idempotent replay
    # can never release the same encumbrance twice.
    if payload.expense_request_id is not None:
        _settle_expense_request(db, payload.expense_request_id, total_debit)
        db.execute(
            update(ExpenseRequest)
            .where(ExpenseRequest.id == payload.expense_request_id)
            .values(journal_entry_id=entry.id)
        )
    return entry, True


def post_journal(db: Session, payload: JournalEntryCreate, actor: str) -> tuple[JournalEntry, bool]:
    entry, created = _post_impl(db, payload, actor)
    db.commit()
    if created:
        db.refresh(entry)
    return entry, created


def reverse_journal(
    db: Session, entry_id: int, year: int, month: int, actor: str, memo: str = ""
) -> JournalEntry:
    """Append a reversing entry (debits/credits swapped) in an open period.

    The original stays untouched. reversal_of_id is unique, so an entry can
    be reversed exactly once; a second attempt is a 409 conflict.
    """
    orig = db.get(JournalEntry, entry_id)
    if orig is None:
        raise DomainError(404, f"journal entry {entry_id} not found")
    period = lock_open_period(db, year, month)
    existing = db.scalar(select(JournalEntry).where(JournalEntry.reversal_of_id == entry_id))
    if existing is not None:
        raise DomainError(
            409, f"journal entry {entry_id} was already reversed by entry {existing.id}"
        )
    reversal = JournalEntry(
        voucher_no=f"REV-{orig.voucher_no}",
        period_id=period.id,
        idempotency_key=f"reversal:{entry_id}",
        content_hash="",
        memo=memo or f"Reversal of voucher {orig.voucher_no}",
        reversal_of_id=orig.id,
        created_by=actor,
    )
    try:
        with db.begin_nested():
            db.add(reversal)
            db.flush()
            for line in orig.lines:
                db.add(
                    JournalLine(
                        entry_id=reversal.id,
                        fund_id=line.fund_id,
                        department_id=line.department_id,
                        account_id=line.account_id,
                        debit_cents=line.credit_cents,
                        credit_cents=line.debit_cents,
                        memo=f"Reversal: {line.memo}" if line.memo else "Reversal",
                    )
                )
            db.flush()
    except IntegrityError:
        raise DomainError(409, f"journal entry {entry_id} was already reversed")
    db.commit()
    db.refresh(reversal)
    return reversal


def _parse_cents(raw: str | None, row_no: int) -> int:
    text = (raw or "").strip()
    if text == "":
        return 0
    try:
        value = int(text)
    except ValueError:
        raise DomainError(422, f"row {row_no}: invalid integer amount {text!r}")
    return value


def import_csv(db: Session, csv_text: str, year: int, month: int, actor: str) -> dict:
    """Import vouchers from CSV. Rows are grouped by voucher_no; every voucher
    must balance. The whole file is one transaction: any error rolls back the
    entire batch. Re-importing the same file is idempotent (vouchers are keyed
    csv:<period>:<voucher_no>); re-importing changed content under the same
    voucher numbers conflicts and aborts the batch.
    """
    reader = csv.DictReader(io.StringIO(csv_text))
    fieldnames = {(c or "").strip() for c in (reader.fieldnames or [])}
    missing = REQUIRED_CSV_COLUMNS - fieldnames
    if missing:
        raise DomainError(422, f"CSV is missing required columns: {sorted(missing)}")
    rows = list(reader)
    if not rows:
        raise DomainError(422, "CSV contains no data rows")
    if len(rows) > MAX_CSV_ROWS:
        raise DomainError(422, f"CSV exceeds the {MAX_CSV_ROWS}-row limit ({len(rows)} rows)")

    groups: dict[str, list[tuple[int, dict]]] = {}
    for row_no, row in enumerate(rows, start=2):  # header is row 1
        voucher = (row.get("voucher_no") or "").strip()
        if not voucher:
            raise DomainError(422, f"row {row_no}: voucher_no is required")
        groups.setdefault(voucher, []).append((row_no, row))

    created: list[str] = []
    reused: list[str] = []
    for voucher, voucher_rows in groups.items():
        lines = []
        for row_no, row in voucher_rows:
            try:
                lines.append(
                    JournalLineIn(
                        fund_code=(row["fund_code"] or "").strip(),
                        department_code=(row["department_code"] or "").strip(),
                        account_code=(row["account_code"] or "").strip(),
                        debit_cents=_parse_cents(row["debit_cents"], row_no),
                        credit_cents=_parse_cents(row["credit_cents"], row_no),
                        memo=(row.get("memo") or "").strip(),
                    )
                )
            except ValidationError as exc:
                raise DomainError(422, f"row {row_no}: {exc.errors()[0]['msg']}")
        payload = JournalEntryCreate(
            year=year,
            month=month,
            voucher_no=voucher,
            memo=f"CSV import voucher {voucher}",
            idempotency_key=f"csv:{year}-{month:02d}:{voucher}",
            lines=lines,
        )
        _, was_created = _post_impl(db, payload, actor)
        (created if was_created else reused).append(voucher)
    db.commit()
    return {"created": created, "reused": reused}


def entry_dict(db: Session, entry: JournalEntry) -> dict:
    rows = db.execute(
        select(JournalLine, Fund.code, Department.code, Account.code)
        .join(Fund, JournalLine.fund_id == Fund.id)
        .join(Department, JournalLine.department_id == Department.id)
        .join(Account, JournalLine.account_id == Account.id)
        .where(JournalLine.entry_id == entry.id)
        .order_by(JournalLine.id)
    ).all()
    lines = [
        {
            "fund_code": fund_code,
            "department_code": dept_code,
            "account_code": acct_code,
            "debit_cents": line.debit_cents,
            "credit_cents": line.credit_cents,
            "memo": line.memo,
        }
        for line, fund_code, dept_code, acct_code in rows
    ]
    return {
        "id": entry.id,
        "voucher_no": entry.voucher_no,
        "year": entry.period.year,
        "month": entry.period.month,
        "memo": entry.memo,
        "idempotency_key": entry.idempotency_key,
        "reversal_of_id": entry.reversal_of_id,
        "expense_request_id": entry.expense_request_id,
        "created_by": entry.created_by,
        "created_at": entry.created_at.isoformat() if entry.created_at else None,
        "total_debit_cents": sum(l["debit_cents"] for l in lines),
        "total_credit_cents": sum(l["credit_cents"] for l in lines),
        "lines": lines,
    }
