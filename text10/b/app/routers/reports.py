import csv
import io

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Account, AuditLog, Budget, Department, Fund
from app.schemas import AuditLogOut, BudgetReportRow
from app.security import Identity, get_identity
from app.services import budget as budget_service

router = APIRouter(tags=["reports"])


def _report_rows(db: Session, year: int | None, fund_id: int | None,
                 department_id: int | None) -> list[BudgetReportRow]:
    stmt = (
        select(Budget, Fund.code, Department.code, Account.code)
        .join(Fund, Budget.fund_id == Fund.id)
        .join(Department, Budget.department_id == Department.id)
        .join(Account, Budget.account_id == Account.id)
        .order_by(Budget.year, Fund.code, Department.code, Account.code)
    )
    if year is not None:
        stmt = stmt.where(Budget.year == year)
    if fund_id is not None:
        stmt = stmt.where(Budget.fund_id == fund_id)
    if department_id is not None:
        stmt = stmt.where(Budget.department_id == department_id)
    rows = []
    for budget, fund_code, dept_code, acct_code in db.execute(stmt):
        rows.append(BudgetReportRow(
            budget_id=budget.id, year=budget.year,
            fund_code=fund_code, department_code=dept_code, account_code=acct_code,
            amount_cents=budget.amount_cents,
            encumbered_cents=budget.encumbered_cents,
            actual_cents=budget.actual_cents,
            available_cents=budget_service.available_cents(budget),
        ))
    return rows


@router.get("/reports/budget", response_model=list[BudgetReportRow])
def budget_report(
    year: int | None = None,
    fund_id: int | None = None,
    department_id: int | None = None,
    db: Session = Depends(get_db),
    _: Identity = Depends(get_identity),
):
    """预算执行报表：预算、预占、实际、剩余额。"""
    return _report_rows(db, year, fund_id, department_id)


@router.get("/reports/budget/export")
def budget_report_export(
    year: int | None = None,
    fund_id: int | None = None,
    department_id: int | None = None,
    db: Session = Depends(get_db),
    _: Identity = Depends(get_identity),
):
    """CSV 导出预算执行报表。"""
    rows = _report_rows(db, year, fund_id, department_id)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["budget_id", "year", "fund", "department", "account",
                     "budget_cents", "encumbered_cents", "actual_cents", "available_cents"])
    for r in rows:
        writer.writerow([r.budget_id, r.year, r.fund_code, r.department_code,
                         r.account_code, r.amount_cents, r.encumbered_cents,
                         r.actual_cents, r.available_cents])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=budget_report.csv"},
    )


@router.get("/audit-log", response_model=list[AuditLogOut])
def list_audit_log(
    limit: int = 100,
    offset: int = 0,
    db: Session = Depends(get_db),
    _: Identity = Depends(get_identity),
):
    stmt = select(AuditLog).order_by(AuditLog.id).limit(min(limit, 500)).offset(offset)
    return [
        AuditLogOut(
            id=a.id, actor=a.actor, action=a.action, entity_type=a.entity_type,
            entity_id=a.entity_id, reason=a.reason, detail=a.detail,
            created_at=a.created_at.isoformat(),
        )
        for a in db.scalars(stmt)
    ]
