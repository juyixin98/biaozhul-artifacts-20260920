"""Seed reference data, fiscal periods and budgets.

Usage:  python -m samples.seed
(uses DATABASE_URL, default postgresql+psycopg://civic:civic@localhost:5432/civicledger)
"""

from app.db import SessionLocal
from app.models import Account, Budget, Department, FiscalPeriod, Fund

FUNDS = [("GEN", "General Fund"), ("CAP", "Capital Projects Fund")]
DEPARTMENTS = [("FIN", "Finance"), ("PARKS", "Parks & Recreation"), ("EDU", "Education")]
ACCOUNTS = [
    ("1001", "Cash", "asset"),
    ("1200", "Accounts Receivable", "asset"),
    ("2001", "Accounts Payable", "liability"),
    ("3001", "Fund Balance", "equity"),
    ("4001", "Tax Revenue", "revenue"),
    ("5001", "Office Supplies", "expense"),
    ("5002", "Equipment", "expense"),
]
BUDGETS = [
    # (year, fund, dept, account, amount_cents)
    (2026, "GEN", "PARKS", "5001", 500_000_00),
    (2026, "GEN", "PARKS", "5002", 1_000_000_00),
    (2026, "GEN", "EDU", "5001", 800_000_00),
    (2026, "CAP", "FIN", "5002", 5_000_000_00),
]


def main() -> None:
    db = SessionLocal()
    try:
        funds = {}
        for code, name in FUNDS:
            f = db.query(Fund).filter_by(code=code).one_or_none() or Fund(code=code, name=name)
            db.add(f)
            db.flush()
            funds[code] = f
        depts = {}
        for code, name in DEPARTMENTS:
            d = db.query(Department).filter_by(code=code).one_or_none() or Department(code=code, name=name)
            db.add(d)
            db.flush()
            depts[code] = d
        accts = {}
        for code, name, atype in ACCOUNTS:
            a = db.query(Account).filter_by(code=code).one_or_none() or Account(
                code=code, name=name, account_type=atype
            )
            db.add(a)
            db.flush()
            accts[code] = a

        for month in range(1, 13):
            if not db.query(FiscalPeriod).filter_by(year=2026, period=month).one_or_none():
                db.add(FiscalPeriod(year=2026, period=month, status="open"))

        for year, fund, dept, acct, amount in BUDGETS:
            exists = (
                db.query(Budget)
                .filter_by(
                    year=year,
                    fund_id=funds[fund].id,
                    department_id=depts[dept].id,
                    account_id=accts[acct].id,
                )
                .one_or_none()
            )
            if not exists:
                db.add(
                    Budget(
                        year=year,
                        fund_id=funds[fund].id,
                        department_id=depts[dept].id,
                        account_id=accts[acct].id,
                        amount_cents=amount,
                    )
                )
        db.commit()
        print("seeded funds, departments, accounts, 2026 periods and budgets")
    finally:
        db.close()


if __name__ == "__main__":
    main()
