from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..database import get_db
from ..errors import DomainError
from ..models import ExpenseRequest
from ..schemas import BudgetCreate, ExpenseRequestCreate
from ..security import ACCOUNTANT, FINANCE_OFFICER, User, get_current_user, require_roles
from ..services import budgets as budget_service

router = APIRouter(tags=["budgets"])

OFFICER = require_roles(FINANCE_OFFICER)
STAFF = require_roles(ACCOUNTANT, FINANCE_OFFICER)


@router.post("/budgets", status_code=201)
def create_budget(
    payload: BudgetCreate,
    db: Session = Depends(get_db),
    user: User = Depends(OFFICER),
):
    return budget_service.budget_dict(budget_service.create_budget(db, payload, user.id))


@router.get("/budgets")
def list_budgets(
    year: int,
    fund_code: str | None = None,
    department_code: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return budget_service.budget_row_dicts(db, year, fund_code, department_code)


@router.post("/expense-requests", status_code=201)
def create_expense_request(
    payload: ExpenseRequestCreate,
    db: Session = Depends(get_db),
    user: User = Depends(STAFF),
):
    return budget_service.request_dict(
        budget_service.create_expense_request(db, payload, user.id)
    )


@router.get("/expense-requests/{request_id}")
def get_expense_request(
    request_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    req = db.get(ExpenseRequest, request_id)
    if req is None:
        raise DomainError(404, f"expense request {request_id} not found")
    return budget_service.request_dict(req)


@router.post("/expense-requests/{request_id}/approve")
def approve_expense_request(
    request_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(OFFICER),
):
    return budget_service.request_dict(
        budget_service.approve_expense_request(db, request_id, user.id)
    )


@router.post("/expense-requests/{request_id}/cancel")
def cancel_expense_request(
    request_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(STAFF),
):
    return budget_service.request_dict(
        budget_service.cancel_expense_request(db, request_id, user.id)
    )
