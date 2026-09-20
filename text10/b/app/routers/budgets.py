from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Budget, ExpenditureRequest
from app.schemas import (
    BudgetCreateIn,
    BudgetOut,
    ExpenditureRequestIn,
    ExpenditureRequestOut,
)
from app.security import Identity, get_identity, require_write
from app.services import budget as budget_service

router = APIRouter(tags=["budgets"])


def _to_out(b: Budget) -> BudgetOut:
    return BudgetOut(
        id=b.id, year=b.year, fund_id=b.fund_id, department_id=b.department_id,
        account_id=b.account_id, amount_cents=b.amount_cents,
        encumbered_cents=b.encumbered_cents, actual_cents=b.actual_cents,
        available_cents=budget_service.available_cents(b),
    )


@router.post("/budgets", response_model=BudgetOut, status_code=201)
def create_budget(
    body: BudgetCreateIn,
    db: Session = Depends(get_db),
    _: Identity = Depends(require_write),
):
    budget = Budget(**body.model_dump())
    db.add(budget)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail={"code": "DUPLICATE_BUDGET",
                                                     "message": "该 年度+基金+部门+科目 的预算已存在"})
    return _to_out(budget)


@router.get("/budgets", response_model=list[BudgetOut])
def list_budgets(
    year: int | None = None,
    fund_id: int | None = None,
    department_id: int | None = None,
    db: Session = Depends(get_db),
    _: Identity = Depends(get_identity),
):
    stmt = select(Budget).order_by(Budget.id)
    if year is not None:
        stmt = stmt.where(Budget.year == year)
    if fund_id is not None:
        stmt = stmt.where(Budget.fund_id == fund_id)
    if department_id is not None:
        stmt = stmt.where(Budget.department_id == department_id)
    return [_to_out(b) for b in db.scalars(stmt)]


@router.get("/budgets/{budget_id}", response_model=BudgetOut)
def get_budget(
    budget_id: int,
    db: Session = Depends(get_db),
    _: Identity = Depends(get_identity),
):
    budget = db.get(Budget, budget_id)
    if budget is None:
        raise HTTPException(status_code=404, detail={"code": "BUDGET_NOT_FOUND",
                                                     "message": f"预算 {budget_id} 不存在"})
    return _to_out(budget)


@router.get("/budgets/{budget_id}/requests", response_model=list[ExpenditureRequestOut])
def list_budget_requests(
    budget_id: int,
    db: Session = Depends(get_db),
    _: Identity = Depends(get_identity),
):
    stmt = (select(ExpenditureRequest)
            .where(ExpenditureRequest.budget_id == budget_id)
            .order_by(ExpenditureRequest.id))
    return [ExpenditureRequestOut.model_validate(r) for r in db.scalars(stmt)]


@router.post("/requests", response_model=ExpenditureRequestOut, status_code=201)
def create_request(
    body: ExpenditureRequestIn,
    db: Session = Depends(get_db),
    identity: Identity = Depends(require_write),
):
    req = budget_service.create_request(
        db, budget_id=body.budget_id, amount_cents=body.amount_cents,
        purpose=body.purpose, actor=identity.name,
    )
    return ExpenditureRequestOut.model_validate(req)


@router.post("/requests/{request_id}/approve", response_model=ExpenditureRequestOut)
def approve_request(
    request_id: int,
    db: Session = Depends(get_db),
    identity: Identity = Depends(require_write),
):
    req = budget_service.approve_request(db, request_id=request_id, actor=identity.name)
    return ExpenditureRequestOut.model_validate(req)


@router.post("/requests/{request_id}/post", response_model=ExpenditureRequestOut)
def post_request(
    request_id: int,
    db: Session = Depends(get_db),
    identity: Identity = Depends(require_write),
):
    req = budget_service.post_request(db, request_id=request_id, actor=identity.name)
    return ExpenditureRequestOut.model_validate(req)


@router.post("/requests/{request_id}/cancel", response_model=ExpenditureRequestOut)
def cancel_request(
    request_id: int,
    db: Session = Depends(get_db),
    identity: Identity = Depends(require_write),
):
    req = budget_service.cancel_request(db, request_id=request_id, actor=identity.name)
    return ExpenditureRequestOut.model_validate(req)
