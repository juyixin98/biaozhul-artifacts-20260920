from __future__ import annotations

from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import settings
from ..errors import ConflictError, NotFoundError, ValidationError
from ..models import (
    Account,
    Budget,
    BudgetReservation,
    Department,
    Fund,
    JournalEntry,
    JournalLine,
    Period,
)
from ..schemas import EntryIn, LineIn, ReversalIn


def _load_references(db: Session, payload: EntryIn) -> dict:
    fund_codes = {line.fund_code for line in payload.lines}
    dept_codes = {line.department_code for line in payload.lines}
    acct_codes = {line.account_code for line in payload.lines}

    funds = {f.code: f for f in db.scalars(select(Fund).where(Fund.code.in_(fund_codes)))}
    depts = {
        d.code: d
        for d in db.scalars(select(Department).where(Department.code.in_(dept_codes)))
    }
    accounts = {
        a.code: a
        for a in db.scalars(select(Account).where(Account.code.in_(acct_codes)))
    }
    return {"funds": funds, "depts": depts, "accounts": accounts}


def _validate_lines(payload: EntryIn, refs: dict) -> None:
    total_debit = 0
    total_credit = 0
    for line in payload.lines:
        if (line.debit_cents > 0) == (line.credit_cents > 0):
            raise ValidationError(
                f"Line {line.line_no}: exactly one of debit_cents/credit_cents "
                "must be positive"
            )
        amount = line.debit_cents or line.credit_cents
        if amount > settings.max_amount_cents:
            raise ValidationError(
                f"Line {line.line_no}: amount {amount} exceeds maximum "
                f"{settings.max_amount_cents} cents"
            )
        total_debit += line.debit_cents
        total_credit += line.credit_cents
        if line.fund_code not in refs["funds"]:
            raise ValidationError(f"Line {line.line_no}: unknown fund {line.fund_code}")
        if line.department_code not in refs["depts"]:
            raise ValidationError(
                f"Line {line.line_no}: unknown department {line.department_code}"
            )
        if line.account_code not in refs["accounts"]:
            raise ValidationError(
                f"Line {line.line_no}: unknown account {line.account_code}"
            )

    if total_debit != total_credit:
        raise ValidationError(
            f"Entry is not balanced: debits {total_debit} != credits {total_credit}"
        )
    if total_debit == 0:
        raise ValidationError("Entry total must be greater than zero")
    if total_debit > settings.max_amount_cents:
        raise ValidationError(
            f"Entry total {total_debit} exceeds maximum "
            f"{settings.max_amount_cents} cents"
        )


def _lock_period(db: Session, payload: EntryIn) -> Period:
    period = db.scalar(select(Period).where(Period.code == payload.period_code).with_for_update())
    if period is None:
        raise ValidationError(f"Period {payload.period_code} does not exist")
    if period.is_closed:
        raise ConflictError(f"Period {period.code} is closed; posting is not allowed")
    if not (period.start_date <= payload.entry_date <= period.end_date):
        raise ValidationError(
            f"entry_date {payload.entry_date} is outside period "
            f"{period.start_date}..{period.end_date}"
        )
    return period


def _load_reservations(db: Session, lines: list[LineIn]) -> dict[str, BudgetReservation]:
    nos = [line.reservation_request_no for line in lines if line.reservation_request_no]
    if not nos:
        return {}
    if len(set(nos)) != len(nos):
        raise ValidationError("A reservation may be referenced by at most one line")
    # Lock the rows by primary key in a deterministic, global order.
    locked_ids = list(
        db.scalars(
            select(BudgetReservation.id)
            .where(BudgetReservation.request_no.in_(nos))
            .order_by(BudgetReservation.id)
            .with_for_update()
        )
    )
    if len(locked_ids) != len(set(nos)):
        found = set(
            db.scalars(
                select(BudgetReservation.request_no).where(
                    BudgetReservation.request_no.in_(nos)
                )
            )
        )
        missing = [n for n in nos if n not in found]
        raise ValidationError(
            "Reservation request(s) do not exist: " + ", ".join(sorted(missing))
        )
    rows = db.scalars(
        select(BudgetReservation).where(BudgetReservation.id.in_(locked_ids))
    ).all()
    return {r.request_no: r for r in rows}


def _budget_key_for_line(line: LineIn, account: Account, year: int) -> tuple:
    return (year, line.fund_code, line.department_code, line.account_code)


def _budget_delta(line: LineIn, account: Account) -> int:
    """Signed budget impact of one line (expense accounts, posted on debit)."""
    if account.account_class != 5:
        return 0
    return line.debit_cents - line.credit_cents


def _apply_budget_impact(
    db: Session,
    payload: EntryIn,
    refs: dict,
    reservations: dict[str, BudgetReservation],
    *,
    is_reversal: bool,
) -> dict[BudgetReservation, JournalLine | None]:
    """
    Lock involved budgets in a deterministic order, verify counters never go
    negative, then apply reserved/actual movements. Returns the map of
    reservations consumed/reversed by these lines.
    """
    year = payload.entry_date.year
    keys: set[tuple] = set()
    line_specs: list[tuple[LineIn, int]] = []
    for line in payload.lines:
        account = refs["accounts"][line.account_code]
        delta = _budget_delta(line, account)
        if delta:
            keys.add(_budget_key_for_line(line, account, year))
        line_specs.append((line, delta))

    reservation_use: dict[BudgetReservation, JournalLine | None] = {}

    # Validate reservation references against their lines first.
    for line, _delta in line_specs:
        if not line.reservation_request_no:
            continue
        reservation = reservations[line.reservation_request_no]
        account = refs["accounts"][line.account_code]
        amount = line.debit_cents or line.credit_cents
        if account.account_class != 5:
            raise ValidationError(
                f"Line {line.line_no}: reservation {reservation.request_no} "
                "must reference an expense account"
            )
        # A forward entry consumes a reservation with a debit on the expense
        # account; a reversal mirrors that as a credit. Both must be one-sided,
        # which _validate_lines already guarantees.
        if is_reversal:
            if line.debit_cents:
                raise ValidationError(
                    f"Line {line.line_no}: reservation {reservation.request_no} "
                    "must be released by a credit line on a reversal"
                )
        else:
            if line.credit_cents:
                raise ValidationError(
                    f"Line {line.line_no}: reservation {reservation.request_no} "
                    "must be consumed by a debit line"
                )
        if amount != reservation.amount_cents:
            raise ValidationError(
                f"Line {line.line_no}: debit {amount} does not match reservation "
                f"{reservation.request_no} amount {reservation.amount_cents}"
            )
        scope = (
            reservation.year,
            reservation.fund_code,
            reservation.department_code,
            reservation.account_code,
        )
        if scope != _budget_key_for_line(line, account, year):
            raise ValidationError(
                f"Line {line.line_no}: reservation {reservation.request_no} scope "
                "does not match the line (year/fund/department/account)"
            )
        expected_status = "consumed" if is_reversal else "approved"
        if reservation.status != expected_status:
            raise ConflictError(
                f"Reservation {reservation.request_no} is '{reservation.status}'; "
                f"expected '{expected_status}'"
            )
        reservation_use[reservation] = None  # line linked after flush
        keys.add(scope)

    # Lock budgets by primary key in a global order to avoid deadlocks.
    budgets: dict[tuple, Budget] = {}
    if keys:
        rows = list(db.scalars(select(Budget)))
        by_key = {
            (b.year, b.fund_code, b.department_code, b.account_code): b for b in rows
        }
        missing = sorted(k for k in keys if k not in by_key)
        if missing:
            raise ValidationError(
                "No budget exists for: "
                + "; ".join(f"{m[0]}/{m[1]}/{m[2]}/{m[3]}" for m in missing)
            )
        locked_ids = sorted(by_key[k].id for k in keys)
        for b in db.scalars(select(Budget).where(Budget.id.in_(locked_ids)).with_for_update()):
            budgets[(b.year, b.fund_code, b.department_code, b.account_code)] = b

    movements: dict[tuple, dict[str, int]] = defaultdict(
        lambda: {"reserved": 0, "actual": 0}
    )
    for line, delta in line_specs:
        if not delta:
            continue
        account = refs["accounts"][line.account_code]
        key = _budget_key_for_line(line, account, year)
        movements[key]["actual"] += delta
        if line.reservation_request_no:
            reservation = reservations[line.reservation_request_no]
            if is_reversal:
                # A reversal restores the pre-occupation when the reservation
                # had already been consumed.
                movements[key]["reserved"] += reservation.amount_cents
            else:
                movements[key]["reserved"] -= reservation.amount_cents

    for key, mv in movements.items():
        budget = budgets[key]
        new_reserved = budget.reserved_cents + mv["reserved"]
        new_actual = budget.actual_cents + mv["actual"]
        if new_reserved < 0:
            raise ConflictError(
                f"Budget {key}: reservation total would go negative ({new_reserved})"
            )
        if new_actual < 0:
            raise ConflictError(
                f"Budget {key}: actual total would go negative ({new_actual})"
            )
        if new_reserved + new_actual > budget.amount_cents:
            raise ConflictError(
                f"Budget {key}: insufficient availability "
                f"(budget {budget.amount_cents}, after move reserved {new_reserved} "
                f"+ actual {new_actual})"
            )
        budget.reserved_cents = new_reserved
        budget.actual_cents = new_actual
        budget.version += 1

    return reservation_use


def create_entry(db: Session, payload: EntryIn, actor: str) -> JournalEntry:
    if db.scalar(select(JournalEntry.id).where(JournalEntry.voucher_no == payload.voucher_no)):
        raise ConflictError(f"Voucher {payload.voucher_no} already exists")

    refs = _load_references(db, payload)
    _validate_lines(payload, refs)
    # Lock order is always: period -> reservations -> budgets.
    _lock_period(db, payload)
    reservations = _load_reservations(db, payload.lines)
    reservation_use = _apply_budget_impact(db, payload, refs, reservations, is_reversal=False)

    entry = JournalEntry(
        voucher_no=payload.voucher_no,
        entry_date=payload.entry_date,
        period_code=payload.period_code,
        description=payload.description,
        created_by=actor,
    )
    db.add(entry)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise ConflictError(f"Voucher {payload.voucher_no} already exists")

    for line in payload.lines:
        reservation = (
            reservations[line.reservation_request_no]
            if line.reservation_request_no
            else None
        )
        row = JournalLine(
            entry_id=entry.id,
            line_no=line.line_no,
            fund_code=line.fund_code,
            department_code=line.department_code,
            account_code=line.account_code,
            debit_cents=line.debit_cents,
            credit_cents=line.credit_cents,
            reservation_id=reservation.id if reservation else None,
            description=line.description,
        )
        db.add(row)
        if reservation is not None:
            reservation_use[reservation] = row

    db.flush()
    for reservation, line in reservation_use.items():
        reservation.status = "consumed"
        reservation.consumed_entry_id = entry.id
    db.flush()
    db.refresh(entry)
    return entry


def reverse_entry(db: Session, payload: ReversalIn, actor: str) -> JournalEntry:
    original = db.scalar(
        select(JournalEntry).where(JournalEntry.voucher_no == payload.voucher_no)
    )
    if original is None:
        raise NotFoundError(f"Voucher {payload.voucher_no} does not exist")
    if original.is_reversal:
        raise ValidationError("A reversal voucher cannot itself be reversed")
    if original.reverses_entry_id is not None:  # defensive; covered by flag
        raise ValidationError("Voucher is already a reversal")
    if original.reversed_by is not None:
        raise ConflictError(
            f"Voucher {payload.voucher_no} already has reversal "
            f"{original.reversed_by.voucher_no}"
        )
    if db.scalar(
        select(JournalEntry.id).where(JournalEntry.voucher_no == payload.voucher_no + "-REV")
    ):
        raise ConflictError("Reversal voucher already exists")

    new_voucher_no = f"{original.voucher_no}-REV"

    period = db.scalar(
        select(Period).where(Period.code == payload.period_code).with_for_update()
    )
    if period is None:
        raise ValidationError(f"Period {payload.period_code} does not exist")
    if period.is_closed:
        raise ConflictError(f"Period {period.code} is closed; posting is not allowed")
    if not (period.start_date <= payload.entry_date <= period.end_date):
        raise ValidationError(
            f"entry_date {payload.entry_date} is outside period "
            f"{period.start_date}..{period.end_date}"
        )

    # Build mirrored lines and reuse the standard budget machinery.
    mirrored: list[LineIn] = []
    for line in original.lines:
        mirrored.append(
            LineIn(
                line_no=line.line_no,
                fund_code=line.fund_code,
                department_code=line.department_code,
                account_code=line.account_code,
                debit_cents=line.credit_cents,
                credit_cents=line.debit_cents,
                reservation_request_no=line.reservation.request_no
                if line.reservation
                else None,
                description=f"Reversal of {original.voucher_no} L{line.line_no}",
            )
        )
    synth = EntryIn(
        voucher_no=new_voucher_no,
        entry_date=payload.entry_date,
        period_code=payload.period_code,
        description=payload.description or f"Reversal of {payload.voucher_no}",
        lines=mirrored,
    )
    refs = _load_references(db, synth)
    _validate_lines(synth, refs)
    reservations = _load_reservations(db, synth.lines)
    _apply_budget_impact(db, synth, refs, reservations, is_reversal=True)

    reversal = JournalEntry(
        voucher_no=new_voucher_no,
        entry_date=payload.entry_date,
        period_code=payload.period_code,
        description=synth.description,
        is_reversal=True,
        reverses_entry_id=original.id,
        reversal_reason=payload.reason,
        created_by=actor,
    )
    db.add(reversal)
    db.flush()

    for line in synth.lines:
        reservation = (
            reservations[line.reservation_request_no]
            if line.reservation_request_no
            else None
        )
        db.add(
            JournalLine(
                entry_id=reversal.id,
                line_no=line.line_no,
                fund_code=line.fund_code,
                department_code=line.department_code,
                account_code=line.account_code,
                debit_cents=line.debit_cents,
                credit_cents=line.credit_cents,
                reservation_id=reservation.id if reservation else None,
                description=line.description,
            )
        )
        if reservation is not None:
            reservation.status = "reversed"
            reservation.reversed_entry_id = reversal.id
    db.flush()
    db.refresh(reversal)
    return reversal
