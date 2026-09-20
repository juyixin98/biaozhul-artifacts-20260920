from __future__ import annotations

import csv
import io

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Budget, BudgetReservation, JournalEntry, JournalLine
from ..security import Principal, require_roles

router = APIRouter(tags=["reports"])

_READERS = require_roles("lead", "accountant", "auditor")


def _usage_rows(
    db: Session,
    *,
    year: int | None,
    fund_code: str | None,
    department_code: str | None,
    account_code: str | None,
) -> list[dict]:
    stmt = select(Budget)
    if year is not None:
        stmt = stmt.where(Budget.year == year)
    if fund_code:
        stmt = stmt.where(Budget.fund_code == fund_code)
    if department_code:
        stmt = stmt.where(Budget.department_code == department_code)
    if account_code:
        stmt = stmt.where(Budget.account_code == account_code)
    stmt = stmt.order_by(
        Budget.year, Budget.fund_code, Budget.department_code, Budget.account_code
    )
    rows = []
    for b in db.scalars(stmt):
        rows.append(
            {
                "year": b.year,
                "fund_code": b.fund_code,
                "department_code": b.department_code,
                "account_code": b.account_code,
                "budget_cents": b.amount_cents,
                "reserved_cents": b.reserved_cents,
                "actual_cents": b.actual_cents,
                "available_cents": b.amount_cents - b.reserved_cents - b.actual_cents,
            }
        )
    return rows


@router.get("/reports/budget-usage")
def budget_usage(
    year: int | None = None,
    fund_code: str | None = None,
    department_code: str | None = None,
    account_code: str | None = None,
    db: Session = Depends(get_db),
    principal: Principal = Depends(_READERS),
):
    return _usage_rows(
        db,
        year=year,
        fund_code=fund_code,
        department_code=department_code,
        account_code=account_code,
    )


@router.get("/reports/budget-usage.csv")
def budget_usage_csv(
    year: int | None = None,
    fund_code: str | None = None,
    department_code: str | None = None,
    account_code: str | None = None,
    db: Session = Depends(get_db),
    principal: Principal = Depends(_READERS),
):
    rows = _usage_rows(
        db,
        year=year,
        fund_code=fund_code,
        department_code=department_code,
        account_code=account_code,
    )
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "year",
            "fund_code",
            "department_code",
            "account_code",
            "budget_cents",
            "reserved_cents",
            "actual_cents",
            "available_cents",
        ]
    )
    for r in rows:
        writer.writerow(
            [
                r["year"],
                r["fund_code"],
                r["department_code"],
                r["account_code"],
                r["budget_cents"],
                r["reserved_cents"],
                r["actual_cents"],
                r["available_cents"],
            ]
        )
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=budget-usage.csv"},
    )


@router.get("/reports/budget-trace")
def budget_trace(
    year: int = Query(...),
    fund_code: str = Query(...),
    department_code: str = Query(...),
    account_code: str = Query(...),
    db: Session = Depends(get_db),
    principal: Principal = Depends(_READERS),
):
    """Every posted line and reservation touching one budget scope, with its
    originating voucher, so figures are traceable back to source documents."""
    line_stmt = (
        select(JournalLine, JournalEntry)
        .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
        .where(
            JournalLine.fund_code == fund_code,
            JournalLine.department_code == department_code,
            JournalLine.account_code == account_code,
        )
        .order_by(JournalEntry.entry_date, JournalEntry.id, JournalLine.line_no)
    )
    lines = []
    for line, entry in db.execute(line_stmt):
        if entry.entry_date.year != year:
            continue
        reservation_no = None
        if line.reservation_id is not None:
            reservation = db.get(BudgetReservation, line.reservation_id)
            reservation_no = reservation.request_no if reservation else None
        reverses_voucher_no = None
        if entry.reverses_entry_id is not None:
            original = db.get(JournalEntry, entry.reverses_entry_id)
            reverses_voucher_no = original.voucher_no if original else None
        lines.append(
            {
                "voucher_no": entry.voucher_no,
                "entry_date": entry.entry_date,
                "period_code": entry.period_code,
                "line_no": line.line_no,
                "debit_cents": line.debit_cents,
                "credit_cents": line.credit_cents,
                "reservation_request_no": reservation_no,
                "is_reversal": entry.is_reversal,
                "reverses_voucher_no": reverses_voucher_no,
            }
        )

    reservation_stmt = select(BudgetReservation).where(
        BudgetReservation.year == year,
        BudgetReservation.fund_code == fund_code,
        BudgetReservation.department_code == department_code,
        BudgetReservation.account_code == account_code,
    )
    reservations = []
    for r in db.scalars(reservation_stmt):
        consumed = db.get(JournalEntry, r.consumed_entry_id) if r.consumed_entry_id else None
        reversed_ = db.get(JournalEntry, r.reversed_entry_id) if r.reversed_entry_id else None
        reservations.append(
            {
                "request_no": r.request_no,
                "amount_cents": r.amount_cents,
                "status": r.status,
                "approved_by": r.approved_by,
                "consumed_voucher_no": consumed.voucher_no if consumed else None,
                "reversed_voucher_no": reversed_.voucher_no if reversed_ else None,
            }
        )

    return {"lines": lines, "reservations": reservations}
