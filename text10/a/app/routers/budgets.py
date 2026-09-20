from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..errors import DomainError
from ..models import Budget, Encumbrance, FiscalPeriod, JournalEntry, JournalLine
from ..schemas import (
    BudgetCreate,
    BudgetOut,
    EncumbranceCreate,
    EncumbranceOut,
)
from ..security import CurrentUser, require_role
from ..services.budgets import approve_encumbrance, cancel_encumbrance

router = APIRouter(tags=["budgets"])

Db = Annotated[Session, Depends(get_db)]
Manager = Annotated[CurrentUser, Depends(require_role("finance_manager"))]
Approver = Annotated[CurrentUser, Depends(require_role("approver", "finance_manager"))]
Reader = Annotated[CurrentUser, Depends(require_role("clerk", "approver", "finance_manager", "auditor"))]


def _budget_json(b: Budget) -> dict:
    return {
        "id": b.id,
        "year": b.year,
        "fund_id": b.fund_id,
        "department_id": b.department_id,
        "account_id": b.account_id,
        "amount_cents": b.amount_cents,
        "encumbered_cents": b.encumbered_cents,
        "actual_cents": b.actual_cents,
        "available_cents": b.amount_cents - b.encumbered_cents - b.actual_cents,
    }


@router.post("/budgets", status_code=201)
def create_budget(payload: BudgetCreate, db: Db, user: Manager):
    exists = db.scalar(
        select(Budget).where(
            Budget.year == payload.year,
            Budget.fund_id == payload.fund_id,
            Budget.department_id == payload.department_id,
            Budget.account_id == payload.account_id,
        )
    )
    if exists is not None:
        raise DomainError(409, "budget-exists", f"budget already exists with id {exists.id}")
    budget = Budget(**payload.model_dump())
    db.add(budget)
    db.commit()
    return _budget_json(budget)


@router.get("/budgets")
def list_budgets(db: Db, user: Reader, year: int | None = None, fund_id: int | None = None):
    stmt = select(Budget).order_by(Budget.id)
    if year is not None:
        stmt = stmt.where(Budget.year == year)
    if fund_id is not None:
        stmt = stmt.where(Budget.fund_id == fund_id)
    return [_budget_json(b) for b in db.scalars(stmt)]


@router.get("/budgets/{budget_id}/activity")
def budget_activity(budget_id: int, db: Db, user: Reader):
    """Traceability: encumbrances and posted journal lines behind a budget."""
    budget = db.get(Budget, budget_id)
    if budget is None:
        raise DomainError(404, "budget-not-found", f"budget {budget_id} does not exist")

    encumbrances = db.scalars(
        select(Encumbrance).where(Encumbrance.budget_id == budget_id).order_by(Encumbrance.id)
    ).all()

    rows = db.execute(
        select(JournalLine, JournalEntry)
        .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
        .join(FiscalPeriod, JournalEntry.period_id == FiscalPeriod.id)
        .where(
            FiscalPeriod.year == budget.year,
            JournalLine.fund_id == budget.fund_id,
            JournalLine.department_id == budget.department_id,
            JournalLine.account_id == budget.account_id,
        )
        .order_by(JournalLine.id)
    ).all()

    return {
        "budget": _budget_json(budget),
        "encumbrances": [EncumbranceOut.model_validate(e).model_dump() for e in encumbrances],
        "journal_lines": [
            {
                "journal_entry_id": entry.id,
                "idempotency_key": entry.idempotency_key,
                "source": entry.source,
                "reversal_of_id": entry.reversal_of_id,
                "line_id": line.id,
                "debit_cents": line.debit_cents,
                "credit_cents": line.credit_cents,
                "created_at": entry.created_at.isoformat(),
            }
            for line, entry in rows
        ],
    }


@router.post("/encumbrances", response_model=EncumbranceOut, status_code=201)
def create_encumbrance(payload: EncumbranceCreate, db: Db, user: Approver):
    encumbrance = approve_encumbrance(db, actor=user.username, **payload.model_dump())
    db.commit()
    return encumbrance


@router.post("/encumbrances/{encumbrance_id}/cancel", response_model=EncumbranceOut)
def cancel_encumbrance_endpoint(encumbrance_id: int, db: Db, user: Approver):
    encumbrance = cancel_encumbrance(db, encumbrance_id, user.username)
    db.commit()
    return encumbrance


@router.get("/encumbrances", response_model=list[EncumbranceOut])
def list_encumbrances(db: Db, user: Reader, status: str | None = None):
    stmt = select(Encumbrance).order_by(Encumbrance.id)
    if status is not None:
        stmt = stmt.where(Encumbrance.status == status)
    return db.scalars(stmt).all()
