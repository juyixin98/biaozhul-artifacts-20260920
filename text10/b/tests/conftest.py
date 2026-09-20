import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base, get_db
from app.main import app
from app.models import Account, Budget, Department, Fund, Period

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://civicledger:civicledger@localhost:56510/civicledger_test",
)

engine = create_engine(TEST_DATABASE_URL, pool_pre_ping=True)
TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

MANAGER = {"Authorization": "Bearer dev-manager-token"}
ACCOUNTANT = {"Authorization": "Bearer dev-accountant-token"}
AUDITOR = {"Authorization": "Bearer dev-auditor-token"}


@pytest.fixture(scope="session", autouse=True)
def create_schema():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)


@pytest.fixture(autouse=True)
def clean_tables():
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())
    yield


@pytest.fixture
def db():
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client():
    def override_get_db():
        session = TestingSessionLocal()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def refs(db):
    """基础主数据：1 基金 / 1 部门 / 2 科目 / 1 开放期间。"""
    fund = Fund(code="GF", name="一般基金")
    dept = Department(code="PW", name="公共工程局")
    cash = Account(code="1001", name="库存现金")
    expense = Account(code="5001", name="办公费支出")
    period = Period(year=2026, month=1, status="open")
    db.add_all([fund, dept, cash, expense, period])
    db.commit()
    return {
        "fund_id": fund.id, "dept_id": dept.id,
        "cash_id": cash.id, "expense_id": expense.id,
        "period_id": period.id,
    }


@pytest.fixture
def budget(db, refs):
    b = Budget(year=2026, fund_id=refs["fund_id"], department_id=refs["dept_id"],
               account_id=refs["expense_id"], amount_cents=1000_00)
    db.add(b)
    db.commit()
    return b


def balanced_body(refs, amount=100_00, period_id=None):
    return {
        "period_id": period_id or refs["period_id"],
        "description": "测试凭证",
        "lines": [
            {"fund_id": refs["fund_id"], "department_id": refs["dept_id"],
             "account_id": refs["expense_id"], "debit_cents": amount, "credit_cents": 0},
            {"fund_id": refs["fund_id"], "department_id": refs["dept_id"],
             "account_id": refs["cash_id"], "debit_cents": 0, "credit_cents": amount},
        ],
    }
