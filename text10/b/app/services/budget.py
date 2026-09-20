"""预算服务：预占 / 转实际 / 取消，全部通过行锁保证并发不超支。"""
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AuditLog, Budget, ExpenditureRequest


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def available_cents(budget: Budget) -> int:
    return budget.amount_cents - budget.encumbered_cents - budget.actual_cents


def _lock_budget(db: Session, budget_id: int) -> Budget:
    budget = db.execute(
        select(Budget).where(Budget.id == budget_id).with_for_update()
    ).scalar_one_or_none()
    if budget is None:
        raise _error(404, "BUDGET_NOT_FOUND", f"预算 {budget_id} 不存在")
    return budget


def _lock_request(db: Session, request_id: int) -> ExpenditureRequest:
    req = db.execute(
        select(ExpenditureRequest)
        .where(ExpenditureRequest.id == request_id)
        .with_for_update()
    ).scalar_one_or_none()
    if req is None:
        raise _error(404, "REQUEST_NOT_FOUND", f"支出申请 {request_id} 不存在")
    return req


def create_request(db: Session, *, budget_id: int, amount_cents: int,
                   purpose: str, actor: str) -> ExpenditureRequest:
    if db.get(Budget, budget_id) is None:
        raise _error(404, "BUDGET_NOT_FOUND", f"预算 {budget_id} 不存在")
    req = ExpenditureRequest(
        budget_id=budget_id, amount_cents=amount_cents,
        purpose=purpose, status="pending", created_by=actor,
    )
    db.add(req)
    db.flush()
    db.add(AuditLog(actor=actor, action="create_request",
                    entity_type="expenditure_request", entity_id=str(req.id),
                    detail=f"amount={amount_cents}"))
    db.commit()
    return req


def approve_request(db: Session, *, request_id: int, actor: str) -> ExpenditureRequest:
    """批准并预占预算。并发安全：申请行与预算行均加锁，可用额不足即拒绝。"""
    req = _lock_request(db, request_id)
    if req.status != "pending":
        raise _error(409, "BAD_STATE", f"申请状态为 {req.status}，不能批准")
    budget = _lock_budget(db, req.budget_id)
    if available_cents(budget) < req.amount_cents:
        raise _error(
            409, "INSUFFICIENT_BUDGET",
            f"可用额 {available_cents(budget)} 分，不足预占 {req.amount_cents} 分",
        )
    budget.encumbered_cents += req.amount_cents
    req.status = "approved"
    db.add(AuditLog(actor=actor, action="approve_request",
                    entity_type="expenditure_request", entity_id=str(req.id)))
    db.commit()
    return req


def post_request(db: Session, *, request_id: int, actor: str) -> ExpenditureRequest:
    """入账：释放预占并形成实际支出。"""
    req = _lock_request(db, request_id)
    if req.status != "approved":
        raise _error(409, "BAD_STATE", f"申请状态为 {req.status}，不能入账")
    budget = _lock_budget(db, req.budget_id)
    budget.encumbered_cents -= req.amount_cents
    budget.actual_cents += req.amount_cents
    req.status = "posted"
    db.add(AuditLog(actor=actor, action="post_request",
                    entity_type="expenditure_request", entity_id=str(req.id)))
    db.commit()
    return req


def cancel_request(db: Session, *, request_id: int, actor: str) -> ExpenditureRequest:
    """取消：预占只释放一次（pending 直接取消，approved 释放预占，其余拒绝）。"""
    req = _lock_request(db, request_id)
    if req.status == "pending":
        req.status = "cancelled"
    elif req.status == "approved":
        budget = _lock_budget(db, req.budget_id)
        budget.encumbered_cents -= req.amount_cents
        req.status = "cancelled"
    else:
        raise _error(409, "BAD_STATE", f"申请状态为 {req.status}，不能取消")
    db.add(AuditLog(actor=actor, action="cancel_request",
                    entity_type="expenditure_request", entity_id=str(req.id)))
    db.commit()
    return req
