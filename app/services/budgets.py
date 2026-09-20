from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..errors import ConflictError, NotFoundError, ValidationError
from ..models import (
    Account,
    Budget,
    BudgetReservation,
    Department,
    Fund,
    JournalEntry,
)
from ..schemas import BudgetIn, ReservationCancelIn, ReservationIn


def _validate_scope(
    db: Session, *, fund_code: str, department_code: str, account_code: str
) -> Account:
    if db.get(Fund, fund_code) is None:
        raise ValidationError(f"Unknown fund {fund_code}")
    if db.get(Department, department_code) is None:
        raise ValidationError(f"Unknown department {department_code}")
    account = db.get(Account, account_code)
    if account is None:
        raise ValidationError(f"Unknown account {account_code}")
    return account


def upsert_budget(db: Session, payload: BudgetIn) -> Budget:
    _validate_scope(
        db,
        fund_code=payload.fund_code,
        department_code=payload.department_code,
        account_code=payload.account_code,
    )
    existing = db.scalar(
        select(Budget)
        .where(
            Budget.year == payload.year,
            Budget.fund_code == payload.fund_code,
            Budget.department_code == payload.department_code,
            Budget.account_code == payload.account_code,
        )
        .with_for_update()
    )
    if existing is not None:
        if payload.amount_cents < existing.reserved_cents + existing.actual_cents:
            raise ConflictError(
                f"New budget {payload.amount_cents} is below already committed "
                f"{existing.reserved_cents + existing.actual_cents}"
            )
        existing.amount_cents = payload.amount_cents
        existing.version += 1
        db.flush()
        return existing

    budget = Budget(
        year=payload.year,
        fund_code=payload.fund_code,
        department_code=payload.department_code,
        account_code=payload.account_code,
        amount_cents=payload.amount_cents,
    )
    db.add(budget)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise ConflictError("Budget for this scope was created concurrently; retry the PUT")
    return budget


def approve_reservation(
    db: Session, payload: ReservationIn, actor: str
) -> BudgetReservation:
    account = _validate_scope(
        db,
        fund_code=payload.fund_code,
        department_code=payload.department_code,
        account_code=payload.account_code,
    )
    if account.account_class != 5:
        raise ValidationError("Reservations may only target expense accounts (class 5)")

    # Lock the budget row first; concurrent approvals serialise on it and the
    # availability check below cannot oversubscribe.
    budget = db.scalar(
        select(Budget)
        .where(
            Budget.year == payload.year,
            Budget.fund_code == payload.fund_code,
            Budget.department_code == payload.department_code,
            Budget.account_code == payload.account_code,
        )
        .with_for_update()
    )
    if budget is None:
        raise NotFoundError(
            "No budget exists for "
            f"{payload.year}/{payload.fund_code}/{payload.department_code}/"
            f"{payload.account_code}"
        )

    # Existence check under the budget lock: two concurrent approvals of the
    # same request number produce a clean 409 instead of an IntegrityError.
    if db.scalar(
        select(BudgetReservation.id).where(
            BudgetReservation.request_no == payload.request_no
        )
    ):
        raise ConflictError(f"Reservation request {payload.request_no} already exists")

    if (
        budget.reserved_cents + budget.actual_cents + payload.amount_cents
        > budget.amount_cents
    ):
        raise ConflictError(
            f"Budget exceeded: available "
            f"{budget.amount_cents - budget.reserved_cents - budget.actual_cents} "
            f"cents, requested {payload.amount_cents}"
        )

    reservation = BudgetReservation(
        request_no=payload.request_no,
        year=payload.year,
        fund_code=payload.fund_code,
        department_code=payload.department_code,
        account_code=payload.account_code,
        amount_cents=payload.amount_cents,
        description=payload.description,
        approved_by=actor,
        status="approved",
    )
    db.add(reservation)
    budget.reserved_cents += payload.amount_cents
    budget.version += 1
    db.flush()
    return reservation


def cancel_reservation(
    db: Session,
    request_no: str,
    payload: ReservationCancelIn,
    actor: str,
) -> BudgetReservation:
    reservation = db.scalar(
        select(BudgetReservation)
        .where(BudgetReservation.request_no == request_no)
        .with_for_update()
    )
    if reservation is None:
        raise NotFoundError(f"Reservation request {request_no} does not exist")
    if reservation.status == "cancelled":
        raise ConflictError("Reservation is already cancelled; release happens once")
    if reservation.status in ("consumed", "reversed"):
        raise ConflictError(
            f"Reservation is '{reservation.status}'; it cannot be cancelled"
        )

    budget = db.scalar(
        select(Budget)
        .where(
            Budget.year == reservation.year,
            Budget.fund_code == reservation.fund_code,
            Budget.department_code == reservation.department_code,
            Budget.account_code == reservation.account_code,
        )
        .with_for_update()
    )
    if budget is None:  # pragma: no cover - cannot happen given approved state
        raise NotFoundError("Budget row backing the reservation is missing")
    budget.reserved_cents -= reservation.amount_cents
    if budget.reserved_cents < 0:  # defensive invariant
        raise ConflictError("Reservation release would make reserved total negative")
    budget.version += 1

    reservation.status = "cancelled"
    reservation.cancelled_by = actor
    reservation.cancel_reason = payload.reason
    reservation.cancelled_at = func.now()
    db.flush()
    return reservation


def _voucher_no(db: Session, entry_id: int | None) -> str | None:
    if entry_id is None:
        return None
    entry = db.get(JournalEntry, entry_id)
    return entry.voucher_no if entry else None


def reservation_to_out(db: Session, reservation: BudgetReservation) -> dict:
    return {
        "id": reservation.id,
        "request_no": reservation.request_no,
        "year": reservation.year,
        "fund_code": reservation.fund_code,
        "department_code": reservation.department_code,
        "account_code": reservation.account_code,
        "amount_cents": reservation.amount_cents,
        "status": reservation.status,
        "description": reservation.description,
        "approved_by": reservation.approved_by,
        "approved_at": reservation.approved_at,
        "cancelled_at": reservation.cancelled_at,
        "cancelled_by": reservation.cancelled_by,
        "cancel_reason": reservation.cancel_reason,
        "consumed_voucher_no": _voucher_no(db, reservation.consumed_entry_id),
        "reversed_voucher_no": _voucher_no(db, reservation.reversed_entry_id),
    }
