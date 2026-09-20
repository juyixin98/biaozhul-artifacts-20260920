"""演示数据种子：基金、部门、科目、期间、预算。幂等，可重复执行。"""
from sqlalchemy import select

from app.database import SessionLocal
from app.models import Account, Budget, Department, Fund, Period

FUNDS = [("GF", "一般公共预算基金"), ("SRF", "专项收入基金")]
DEPARTMENTS = [("PW", "公共工程局"), ("EDU", "教育局"), ("FIN", "财政局")]
ACCOUNTS = [
    ("1001", "库存现金"),
    ("5001", "办公费支出"),
    ("5002", "设备购置支出"),
    ("6001", "财政拨款收入"),
]
PERIODS = [(2026, m) for m in range(1, 13)]
BUDGET_YEAR = 2026
BUDGET_AMOUNT = 1_000_000_00  # 100 万元（分）


def seed() -> None:
    db = SessionLocal()
    try:
        funds = {}
        for code, name in FUNDS:
            f = db.scalar(select(Fund).where(Fund.code == code)) or Fund(code=code, name=name)
            db.add(f)
            db.flush()
            funds[code] = f
        depts = {}
        for code, name in DEPARTMENTS:
            d = db.scalar(select(Department).where(Department.code == code)) or Department(code=code, name=name)
            db.add(d)
            db.flush()
            depts[code] = d
        accts = {}
        for code, name in ACCOUNTS:
            a = db.scalar(select(Account).where(Account.code == code)) or Account(code=code, name=name)
            db.add(a)
            db.flush()
            accts[code] = a
        for year, month in PERIODS:
            if not db.scalar(select(Period).where(Period.year == year, Period.month == month)):
                db.add(Period(year=year, month=month, status="open"))
        db.flush()
        # 每个部门在一般基金下给办公费/设备费各配一份预算
        for dept in depts.values():
            for acct_code in ("5001", "5002"):
                exists = db.scalar(select(Budget).where(
                    Budget.year == BUDGET_YEAR,
                    Budget.fund_id == funds["GF"].id,
                    Budget.department_id == dept.id,
                    Budget.account_id == accts[acct_code].id,
                ))
                if not exists:
                    db.add(Budget(
                        year=BUDGET_YEAR, fund_id=funds["GF"].id,
                        department_id=dept.id, account_id=accts[acct_code].id,
                        amount_cents=BUDGET_AMOUNT,
                    ))
        db.commit()
        print("seed done")
    finally:
        db.close()


if __name__ == "__main__":
    seed()
