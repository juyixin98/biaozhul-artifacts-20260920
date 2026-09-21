import csv
import io

from fastapi import APIRouter, Depends, Response
from sqlalchemy.orm import Session

from ..database import get_db
from ..security import User, get_current_user
from ..services import budgets as budget_service

router = APIRouter(prefix="/reports", tags=["reports"])

EXPORT_COLUMNS = [
    "year",
    "fund_code",
    "department_code",
    "account_code",
    "budget_cents",
    "encumbered_cents",
    "actual_cents",
    "available_cents",
]


@router.get("/budget")
def budget_report(
    year: int,
    fund_code: str | None = None,
    department_code: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return {
        "year": year,
        "rows": budget_service.budget_row_dicts(db, year, fund_code, department_code),
    }


@router.get("/budget/export")
def budget_export(
    year: int,
    fund_code: str | None = None,
    department_code: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    rows = budget_service.budget_row_dicts(db, year, fund_code, department_code)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(EXPORT_COLUMNS)
    for row in rows:
        writer.writerow([row[col] for col in EXPORT_COLUMNS])
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="budget_{year}.csv"'},
    )
