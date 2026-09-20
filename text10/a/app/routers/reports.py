import csv
import io
from typing import Annotated

from fastapi import APIRouter, Depends, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Account, Budget, Department, Fund
from ..security import CurrentUser, require_role

router = APIRouter(prefix="/reports", tags=["reports"])

Db = Annotated[Session, Depends(get_db)]
Reader = Annotated[CurrentUser, Depends(require_role("clerk", "approver", "finance_manager", "auditor"))]

COLUMNS = [
    "year", "fund_code", "department_code", "account_code",
    "budget_cents", "encumbered_cents", "actual_cents", "available_cents",
]


def _rows(db: Session, year: int, fund_id: int | None, department_id: int | None):
    stmt = (
        select(Budget, Fund, Department, Account)
        .join(Fund, Budget.fund_id == Fund.id)
        .join(Department, Budget.department_id == Department.id)
        .join(Account, Budget.account_id == Account.id)
        .where(Budget.year == year)
        .order_by(Fund.code, Department.code, Account.code)
    )
    if fund_id is not None:
        stmt = stmt.where(Budget.fund_id == fund_id)
    if department_id is not None:
        stmt = stmt.where(Budget.department_id == department_id)
    out = []
    for b, f, d, a in db.execute(stmt):
        out.append(
            {
                "year": b.year,
                "budget_id": b.id,
                "fund_code": f.code,
                "department_code": d.code,
                "account_code": a.code,
                "budget_cents": b.amount_cents,
                "encumbered_cents": b.encumbered_cents,
                "actual_cents": b.actual_cents,
                "available_cents": b.amount_cents - b.encumbered_cents - b.actual_cents,
            }
        )
    return out


@router.get("/budget")
def budget_report(year: int, db: Db, user: Reader, fund_id: int | None = None, department_id: int | None = None):
    return {"year": year, "rows": _rows(db, year, fund_id, department_id)}


@router.get("/budget/export")
def budget_report_csv(year: int, db: Db, user: Reader, fund_id: int | None = None, department_id: int | None = None):
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(COLUMNS)
    for row in _rows(db, year, fund_id, department_id):
        writer.writerow([row[c] for c in COLUMNS])
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="budget_{year}.csv"'},
    )
