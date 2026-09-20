from __future__ import annotations

import hashlib
import json

from sqlalchemy import select

from ..errors import DomainError
from ..models import (
    Account,
    Budget,
    Department,
    Encumbrance,
    FiscalPeriod,
    Fund,
    JournalEntry,
    JournalLine,
)
from ..schemas import JournalCreate, JournalLineIn, ReversalCreate
from ..models import utcnow


def payload_digest(payload: JournalCreate) -> str:
    canonical = json.dumps(
        payload.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def lock_open_period(db, period_id: int) -> FiscalPeriod:
    """Serialize with period close: whoever takes the row lock first wins."""
    period = db.scalar(
        select(FiscalPeriod).where(FiscalPeriod.id == period_id).with_for_update()
    )
    if period is None:
        raise DomainError(404, "period-not-found", f"period {period_id} does not exist")
    if period.status != "open":
        raise DomainError(
            409,
            "period-closed",
            f"period {period.year}-{period.period:02d} is closed",
        )
    return period


def _validate_references(db, lines: list[JournalLineIn]) -> dict[int, Account]:
    fund_ids = {l.fund_id for l in lines}
    dept_ids = {l.department_id for l in lines}
    acct_ids = {l.account_id for l in lines}

    funds = set(db.scalars(select(Fund.id).where(Fund.id.in_(fund_ids))))
    depts = set(db.scalars(select(Department.id).where(Department.id.in_(dept_ids))))
    accounts = {
        a.id: a for a in db.scalars(select(Account).where(Account.id.in_(acct_ids)))
    }

    problems = []
    for l in lines:
        if l.fund_id not in funds:
            problems.append(f"fund {l.fund_id} does not exist")
        if l.department_id not in depts:
            problems.append(f"department {l.department_id} does not exist")
        if l.account_id not in accounts:
            problems.append(f"account {l.account_id} does not exist")
    if problems:
        raise DomainError(422, "invalid-reference", problems)
    return accounts


def _apply_budget_effects(
    db,
    year: int,
    lines: list[JournalLine],
    accounts: dict[int, Account],
    encumbrance: Encumbrance | None,
) -> None:
    """Apply actuals (and encumbrance release) to matching budget rows.

    Only expense accounts participate in budget control. Budget rows are
    locked with SELECT ... FOR UPDATE so concurrent postings serialize.
    """
    deltas: dict[int, list] = {}  # budget id -> [Budget, actual_delta]

    def budget_for(line: JournalLine) -> Budget | None:
        return db.scalar(
            select(Budget)
            .where(
                Budget.year == year,
                Budget.fund_id == line.fund_id,
                Budget.department_id == line.department_id,
                Budget.account_id == line.account_id,
            )
            .with_for_update()
        )

    for line in lines:
        if accounts[line.account_id].account_type != "expense":
            continue
        budget = budget_for(line)
        if budget is None:
            continue
        slot = deltas.setdefault(budget.id, [budget, 0])
        slot[1] += line.debit_cents - line.credit_cents

    if encumbrance is not None:
        budget = db.scalar(
            select(Budget).where(Budget.id == encumbrance.budget_id).with_for_update()
        )
        slot = deltas.setdefault(budget.id, [budget, 0])
        if slot[1] != encumbrance.amount_cents:
            raise DomainError(
                422,
                "encumbrance-amount-mismatch",
                "expense lines on the encumbrance's budget dimensions must total "
                f"the encumbrance amount ({encumbrance.amount_cents} cents)",
            )
        budget.encumbered_cents -= encumbrance.amount_cents

    for budget, delta in deltas.values():
        budget.actual_cents += delta
        if (
            budget.encumbered_cents < 0
            or budget.amount_cents - budget.encumbered_cents - budget.actual_cents < 0
        ):
            raise DomainError(
                409,
                "budget-exceeded",
                f"budget {budget.id} would be exceeded "
                f"(available {budget.amount_cents - budget.encumbered_cents - budget.actual_cents + 0} "
                f"after applying {delta} cents)",
            )


def post_journal(
    db,
    payload: JournalCreate,
    actor: str,
    *,
    source: str = "manual",
    reversal_of_id: int | None = None,
) -> tuple[JournalEntry, bool]:
    """Post a balanced journal entry. Returns (entry, replayed).

    Idempotent on ``payload.idempotency_key``: an identical retry returns the
    original entry; the same key with different content is a 409 conflict.
    """
    digest = payload_digest(payload)

    existing = db.scalar(
        select(JournalEntry).where(JournalEntry.idempotency_key == payload.idempotency_key)
    )
    if existing is not None:
        if existing.payload_hash == digest:
            return existing, True
        raise DomainError(
            409,
            "idempotency-key-conflict",
            "idempotency key was already used with a different payload",
        )

    period = lock_open_period(db, payload.period_id)
    accounts = _validate_references(db, payload.lines)

    total_debit = sum(l.debit_cents for l in payload.lines)
    total_credit = sum(l.credit_cents for l in payload.lines)
    if total_debit == 0 or total_debit != total_credit:
        raise DomainError(
            422,
            "unbalanced-entry",
            f"debits ({total_debit}) must equal credits ({total_credit}) and be > 0",
        )

    encumbrance = None
    if payload.encumbrance_id is not None:
        encumbrance = db.scalar(
            select(Encumbrance)
            .where(Encumbrance.id == payload.encumbrance_id)
            .with_for_update()
        )
        if encumbrance is None:
            raise DomainError(
                404, "encumbrance-not-found", f"encumbrance {payload.encumbrance_id} does not exist"
            )
        if encumbrance.status != "open":
            raise DomainError(
                409,
                "encumbrance-not-open",
                f"encumbrance {encumbrance.id} is {encumbrance.status}, cannot liquidate",
            )

    entry = JournalEntry(
        idempotency_key=payload.idempotency_key,
        payload_hash=digest,
        period_id=period.id,
        memo=payload.memo,
        source=source,
        reversal_of_id=reversal_of_id,
        created_by=actor,
    )
    db.add(entry)
    db.flush()  # assign id; unique(idempotency_key) surfaces races as IntegrityError

    lines = [
        JournalLine(
            entry_id=entry.id,
            fund_id=l.fund_id,
            department_id=l.department_id,
            account_id=l.account_id,
            debit_cents=l.debit_cents,
            credit_cents=l.credit_cents,
        )
        for l in payload.lines
    ]
    db.add_all(lines)
    db.flush()

    _apply_budget_effects(db, period.year, lines, accounts, encumbrance)

    if encumbrance is not None:
        encumbrance.status = "liquidated"
        encumbrance.journal_entry_id = entry.id
        encumbrance.closed_at = utcnow()

    db.flush()
    return entry, False


def reverse_journal(
    db, original_id: int, payload: ReversalCreate, actor: str
) -> tuple[JournalEntry, bool]:
    """Append a reversing entry for a posted entry, in an open period.

    Each entry can be reversed at most once (enforced by the unique
    constraint on journal_entries.reversal_of_id as well).
    """
    original = db.get(JournalEntry, original_id)
    if original is None:
        raise DomainError(404, "entry-not-found", f"journal entry {original_id} does not exist")

    mirrored = [
        JournalLineIn(
            fund_id=l.fund_id,
            department_id=l.department_id,
            account_id=l.account_id,
            debit_cents=l.credit_cents,
            credit_cents=l.debit_cents,
        )
        for l in original.lines
    ]
    create = JournalCreate(
        idempotency_key=payload.idempotency_key,
        period_id=payload.period_id,
        memo=payload.memo or f"Reversal of entry {original_id}",
        lines=mirrored,
    )

    # Idempotent replay of the same reversal request wins over the
    # already-reversed check below.
    digest = payload_digest(create)
    existing = db.scalar(
        select(JournalEntry).where(JournalEntry.idempotency_key == create.idempotency_key)
    )
    if existing is not None:
        if existing.payload_hash == digest:
            return existing, True
        raise DomainError(
            409,
            "idempotency-key-conflict",
            "idempotency key was already used with a different payload",
        )

    existing_reversal = db.scalar(
        select(JournalEntry).where(JournalEntry.reversal_of_id == original_id)
    )
    if existing_reversal is not None:
        raise DomainError(
            409,
            "already-reversed",
            f"entry {original_id} was already reversed by entry {existing_reversal.id}",
        )

    return post_journal(db, create, actor, source="reversal", reversal_of_id=original_id)
