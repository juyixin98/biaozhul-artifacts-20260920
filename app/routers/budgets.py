from __future__ import annotations

from fastapi import APIRouter, Depends, Header
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Budget, BudgetReservation
from ..schemas import (
    BudgetIn,
    BudgetOut,
    ReservationCancelIn,
    ReservationIn,
    ReservationOut,
)
from ..security import Principal, require_roles
from ..services import run_idempotent
from ..services.budgets import (
    approve_reservation,
    cancel_reservation,
    reservation_to_out,
    upsert_budget,
)

router = APIRouter(tags=["budgets"])

_READERS = require_roles("lead", "accountant", "auditor")
_LEAD = require_roles("lead")


def _budget_out(b: Budget) -> dict:
    return {
        "id": b.id,
        "year": b.year,
        "fund_code": b.fund_code,
        "department_code": b.department_code,
        "account_code": b.account_code,
        "amount_cents": b.amount_cents,
        "reserved_cents": b.reserved_cents,
        "actual_cents": b.actual_cents,
        "available_cents": b.amount_cents - b.reserved_cents - b.actual_cents,
    }


@router.put("/budgets", response_model=BudgetOut)
def put_budget(
    payload: BudgetIn,
    db: Session = Depends(get_db),
    principal: Principal = Depends(_LEAD),
):
    budget = upsert_budget(db, payload)
    db.commit()
    return _budget_out(budget)


@router.get("/budgets", response_model=list[BudgetOut])
def list_budgets(
    year: int | None = None,
    fund_code: str | None = None,
    department_code: str | None = None,
    account_code: str | None = None,
    db: Session = Depends(get_db),
    principal: Principal = Depends(_READERS),
):
    stmt = select(Budget).order_by(
        Budget.year, Budget.fund_code, Budget.department_code, Budget.account_code
    )
    if year is not None:
        stmt = stmt.where(Budget.year == year)
    if fund_code:
        stmt = stmt.where(Budget.fund_code == fund_code)
    if department_code:
        stmt = stmt.where(Budget.department_code == department_code)
    if account_code:
        stmt = stmt.where(Budget.account_code == account_code)
    return [_budget_out(b) for b in db.scalars(stmt)]


@router.post("/reservations", response_model=ReservationOut, status_code=201)
def post_reservation(
    payload: ReservationIn,
    db: Session = Depends(get_db),
    principal: Principal = Depends(_LEAD),
    idempotency_key: str | None = Header(default=None, max_length=128),
):
    def handler():
        reservation = approve_reservation(db, payload, actor=principal.name)
        return 201, reservation_to_out(db, reservation)

    _status, body, _replayed = run_idempotent(
        db, idempotency_key, payload.model_dump(mode="json"), handler
    )
    return body


@router.post("/reservations/{request_no}/cancel", response_model=ReservationOut)
def cancel_one(
    request_no: str,
    payload: ReservationCancelIn,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_roles("lead", "accountant")),
    idempotency_key: str | None = Header(default=None, max_length=128),
):
    def handler():
        reservation = cancel_reservation(db, request_no, payload, actor=principal.name)
        return 200, reservation_to_out(db, reservation)

    _status, body, _replayed = run_idempotent(
        db, idempotency_key, payload.model_dump(mode="json"), handler
    )
    return body


@router.get("/reservations", response_model=list[ReservationOut])
def list_reservations(
    status: str | None = None,
    request_no: str | None = None,
    db: Session = Depends(get_db),
    principal: Principal = Depends(_READERS),
):
    stmt = select(BudgetReservation).order_by(BudgetReservation.id.desc())
    if status:
        stmt = stmt.where(BudgetReservation.status == status)
    if request_no:
        stmt = stmt.where(BudgetReservation.request_no == request_no)
    return [reservation_to_out(db, r) for r in db.scalars(stmt)]
