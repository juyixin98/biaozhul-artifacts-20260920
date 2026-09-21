"""Budgets and expense requests (encumbrance lifecycle).

All balance mutations are atomic guarded UPDATEs (`UPDATE ... WHERE guard`),
so concurrent approvals can never push encumbered + actual above the budget
amount, and a cancel releases its encumbrance exactly once. The CHECK
constraints on the budgets table are the database-level backstop.
"""
from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..errors import DomainError
from ..models import Account, Budget, Department, ExpenseRequest, Fund
from ..schemas import BudgetCreate, ExpenseRequestCreate


def create_budget(db: Session, payload: BudgetCreate, actor: str) -> Budget:
    fund = db.scalar(select(Fund).where(Fund.code == payload.fund_code))
    department = db.scalar(select(Department).where(Department.code == payload.department_code))
    account = db.scalar(select(Account).where(Account.code == payload.account_code))
    for label, row in (("fund", fund), ("department", department), ("account", account)):
        if row is None:
            raise DomainError(422, f"unknown {label} code")
    budget = Budget(
        year=payload.year,
        fund_id=fund.id,
        department_id=department.id,
        account_id=account.id,
        amount_cents=payload.amount_cents,
    )
    db.add(budget)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise DomainError(409, "a budget line already exists for this year/fund/department/account")
    db.refresh(budget)
    return budget


def create_expense_request(db: Session, payload: ExpenseRequestCreate, actor: str) -> ExpenseRequest:
    if db.get(Budget, payload.budget_id) is None:
        raise DomainError(404, f"budget {payload.budget_id} not found")
    req = ExpenseRequest(
        budget_id=payload.budget_id,
        amount_cents=payload.amount_cents,
        purpose=payload.purpose,
        created_by=actor,
    )
    db.add(req)
    db.commit()
    db.refresh(req)
    return req


def approve_expense_request(db: Session, request_id: int, actor: str) -> ExpenseRequest:
    """pending -> approved, reserving (encumbering) the amount atomically."""
    req = db.get(ExpenseRequest, request_id)
    if req is None:
        raise DomainError(404, f"expense request {request_id} not found")
    res = db.execute(
        update(ExpenseRequest)
        .where(ExpenseRequest.id == request_id, ExpenseRequest.status == "pending")
        .values(status="approved")
    )
    if res.rowcount != 1:
        raise DomainError(
            409, f"expense request {request_id} cannot be approved from status {req.status!r}"
        )
    res = db.execute(
        update(Budget)
        .where(
            Budget.id == req.budget_id,
            Budget.encumbered_cents + Budget.actual_cents + req.amount_cents
            <= Budget.amount_cents,
        )
        .values(encumbered_cents=Budget.encumbered_cents + req.amount_cents)
    )
    if res.rowcount != 1:
        # Rolls back the status change above as well (no commit happened).
        raise DomainError(409, "insufficient available budget")
    db.commit()
    db.refresh(req)
    return req


def cancel_expense_request(db: Session, request_id: int, actor: str) -> ExpenseRequest:
    """pending/approved -> cancelled. The encumbrance is released exactly once:
    the status transition is atomic, so a concurrent or repeated cancel finds
    the request already cancelled and releases nothing."""
    req = db.get(ExpenseRequest, request_id)
    if req is None:
        raise DomainError(404, f"expense request {request_id} not found")
    if req.status == "pending":
        res = db.execute(
            update(ExpenseRequest)
            .where(ExpenseRequest.id == request_id, ExpenseRequest.status == "pending")
            .values(status="cancelled")
        )
        if res.rowcount != 1:
            raise DomainError(409, f"expense request {request_id} was already changed")
    elif req.status == "approved":
        res = db.execute(
            update(ExpenseRequest)
            .where(ExpenseRequest.id == request_id, ExpenseRequest.status == "approved")
            .values(status="cancelled")
        )
        if res.rowcount != 1:
            raise DomainError(409, f"expense request {request_id} was already changed")
        res = db.execute(
            update(Budget)
            .where(Budget.id == req.budget_id, Budget.encumbered_cents >= req.amount_cents)
            .values(encumbered_cents=Budget.encumbered_cents - req.amount_cents)
        )
        if res.rowcount != 1:
            raise DomainError(409, "encumbrance balance inconsistency while cancelling")
    else:
        raise DomainError(
            409, f"expense request {request_id} cannot be cancelled from status {req.status!r}"
        )
    db.commit()
    db.refresh(req)
    return req


def budget_row_dicts(
    db: Session, year: int, fund_code: str | None = None, department_code: str | None = None
) -> list[dict]:
    stmt = (
        select(Budget, Fund.code, Department.code, Account.code)
        .join(Fund, Budget.fund_id == Fund.id)
        .join(Department, Budget.department_id == Department.id)
        .join(Account, Budget.account_id == Account.id)
        .where(Budget.year == year)
        .order_by(Fund.code, Department.code, Account.code)
    )
    if fund_code:
        stmt = stmt.where(Fund.code == fund_code)
    if department_code:
        stmt = stmt.where(Department.code == department_code)
    rows = []
    for budget, f_code, d_code, a_code in db.execute(stmt).all():
        rows.append(
            {
                "budget_id": budget.id,
                "year": budget.year,
                "fund_code": f_code,
                "department_code": d_code,
                "account_code": a_code,
                "budget_cents": budget.amount_cents,
                "encumbered_cents": budget.encumbered_cents,
                "actual_cents": budget.actual_cents,
                "available_cents": budget.amount_cents
                - budget.encumbered_cents
                - budget.actual_cents,
            }
        )
    return rows


def budget_dict(budget: Budget) -> dict:
    return {
        "id": budget.id,
        "year": budget.year,
        "fund_code": budget.fund.code,
        "department_code": budget.department.code,
        "account_code": budget.account.code,
        "budget_cents": budget.amount_cents,
        "encumbered_cents": budget.encumbered_cents,
        "actual_cents": budget.actual_cents,
        "available_cents": budget.amount_cents - budget.encumbered_cents - budget.actual_cents,
    }


def request_dict(req: ExpenseRequest) -> dict:
    return {
        "id": req.id,
        "budget_id": req.budget_id,
        "amount_cents": req.amount_cents,
        "status": req.status,
        "purpose": req.purpose,
        "journal_entry_id": req.journal_entry_id,
        "created_by": req.created_by,
        "created_at": req.created_at.isoformat() if req.created_at else None,
    }
