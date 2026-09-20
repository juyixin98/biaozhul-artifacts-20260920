"""Idempotent sample data: funds, departments, accounts, periods, one budget,
one approved reservation and one balanced posted voucher.

Run: python -m app.seed
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import select

from .database import Base, SessionLocal, engine
from .models import (
    Account,
    Budget,
    BudgetReservation,
    Department,
    Fund,
    JournalEntry,
    JournalLine,
    Period,
)


def _get_or_create(db, model, code: str, **values):
    obj = db.get(model, code)
    if obj is not None:
        return obj
    obj = model(code=code, **values)
    db.add(obj)
    db.flush()
    return obj


def seed() -> None:
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        _get_or_create(db, Fund, "GF", name="General Fund")
        _get_or_create(db, Fund, "SF", name="Special Revenue Fund")
        _get_or_create(db, Department, "ADMIN", name="Administration")
        _get_or_create(db, Department, "PARKS", name="Parks & Recreation")

        _get_or_create(
            db, Account, "1010", name="Cash", account_class=1, normal_side="D"
        )
        _get_or_create(
            db, Account, "2010", name="Accounts Payable", account_class=2, normal_side="C"
        )
        _get_or_create(
            db, Account, "5100", name="Supplies Expense", account_class=5, normal_side="D"
        )
        _get_or_create(
            db, Account, "5200", name="Contracted Services", account_class=5, normal_side="D"
        )

        _get_or_create(
            db,
            Period,
            "2026-09",
            start_date=date(2026, 9, 1),
            end_date=date(2026, 9, 30),
            is_closed=False,
        )

        existing_budget = db.scalar(
            select(Budget).where(
                Budget.year == 2026,
                Budget.fund_code == "GF",
                Budget.department_code == "ADMIN",
                Budget.account_code == "5100",
            )
        )
        if existing_budget is None:
            db.add(
                Budget(
                    year=2026,
                    fund_code="GF",
                    department_code="ADMIN",
                    account_code="5100",
                    amount_cents=10_000_00,  # 10,000.00
                )
            )
            db.flush()

        if not db.scalar(
            select(Budget).where(
                Budget.year == 2026,
                Budget.fund_code == "GF",
                Budget.department_code == "ADMIN",
                Budget.account_code == "5200",
            )
        ):
            db.add(
                Budget(
                    year=2026,
                    fund_code="GF",
                    department_code="ADMIN",
                    account_code="5200",
                    amount_cents=5_000_00,
                )
            )
            db.flush()

        if not db.scalar(
            select(BudgetReservation).where(BudgetReservation.request_no == "REQ-0001")
        ):
            budget = db.scalar(
                select(Budget).where(
                    Budget.year == 2026,
                    Budget.fund_code == "GF",
                    Budget.department_code == "ADMIN",
                    Budget.account_code == "5100",
                )
            )
            db.add(
                BudgetReservation(
                    request_no="REQ-0001",
                    year=2026,
                    fund_code="GF",
                    department_code="ADMIN",
                    account_code="5100",
                    amount_cents=2_500_00,  # 2,500.00 pre-occupied
                    description="Quarterly office supplies",
                    approved_by="lead",
                    status="approved",
                )
            )
            budget.reserved_cents += 2_500_00

        if not db.scalar(
            select(JournalEntry).where(JournalEntry.voucher_no == "JV-SEED-0001")
        ):
            entry = JournalEntry(
                voucher_no="JV-SEED-0001",
                entry_date=date(2026, 9, 2),
                period_code="2026-09",
                description="Opening supplies purchase (sample)",
                created_by="seed",
            )
            db.add(entry)
            db.flush()
            reservation = db.scalar(
                select(BudgetReservation).where(
                    BudgetReservation.request_no == "REQ-0001"
                )
            )
            db.add_all(
                [
                    JournalLine(
                        entry_id=entry.id,
                        line_no=1,
                        fund_code="GF",
                        department_code="ADMIN",
                        account_code="5100",
                        debit_cents=2_500_00,
                        reservation_id=reservation.id,
                        description="Supplies delivered",
                    ),
                    JournalLine(
                        entry_id=entry.id,
                        line_no=2,
                        fund_code="GF",
                        department_code="ADMIN",
                        account_code="1010",
                        credit_cents=2_500_00,
                        description="Paid from cash",
                    ),
                ]
            )
            reservation.status = "consumed"
            reservation.consumed_entry_id = entry.id
            budget = db.scalar(
                select(Budget).where(
                    Budget.year == 2026,
                    Budget.fund_code == "GF",
                    Budget.department_code == "ADMIN",
                    Budget.account_code == "5100",
                )
            )
            budget.reserved_cents -= 2_500_00
            budget.actual_cents += 2_500_00

        db.commit()
        print("Seed complete.")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    seed()
