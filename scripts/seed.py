"""Load demo reference data, periods and budgets. Idempotent: if any fund
already exists the script assumes the database was seeded and does nothing.

Run with:  python -m scripts.seed
"""
from sqlalchemy import select

from app.database import Base, SessionLocal, engine
from app.models import Account, Budget, Department, Fund, Period

FUNDS = [
    ("GEN", "General Fund"),
    ("CAP", "Capital Projects Fund"),
]
DEPARTMENTS = [
    ("FIN", "Finance"),
    ("PRK", "Parks & Recreation"),
    ("EDU", "Education"),
]
ACCOUNTS = [
    ("1000", "Cash"),
    ("2000", "Accounts Payable"),
    ("5000", "Supplies Expense"),
    ("6000", "Equipment Expense"),
]
# (fund, department, account, amount_cents)
BUDGETS = [
    ("GEN", "FIN", "5000", 500_000_00),
    ("GEN", "PRK", "5000", 200_000_00),
    ("GEN", "PRK", "6000", 1_000_000_00),
    ("CAP", "EDU", "6000", 2_000_000_00),
]
PERIODS = [(2026, 9), (2026, 10)]


def main() -> None:
    Base.metadata.create_all(engine)  # dev convenience; migrations are authoritative
    db = SessionLocal()
    try:
        if db.scalar(select(Fund).limit(1)):
            print("seed: reference data already present, skipping")
            return
        funds = {code: Fund(code=code, name=name) for code, name in FUNDS}
        departments = {code: Department(code=code, name=name) for code, name in DEPARTMENTS}
        accounts = {code: Account(code=code, name=name) for code, name in ACCOUNTS}
        db.add_all([*funds.values(), *departments.values(), *accounts.values()])
        db.flush()
        for year, month in PERIODS:
            db.add(Period(year=year, month=month))
        for fund_code, dept_code, acct_code, amount in BUDGETS:
            db.add(
                Budget(
                    year=2026,
                    fund_id=funds[fund_code].id,
                    department_id=departments[dept_code].id,
                    account_id=accounts[acct_code].id,
                    amount_cents=amount,
                )
            )
        db.commit()
        print(
            f"seed: {len(funds)} funds, {len(departments)} departments, "
            f"{len(accounts)} accounts, {len(PERIODS)} periods, {len(BUDGETS)} budget lines"
        )
    finally:
        db.close()


if __name__ == "__main__":
    main()
