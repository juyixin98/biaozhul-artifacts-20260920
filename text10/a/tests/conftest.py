import os
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import get_db
from app.main import app
from app.models import (
    Account,
    Base,
    Budget,
    Department,
    FiscalPeriod,
    Fund,
)

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://civic:civic@localhost:5432/civicledger_test",
)

engine = create_engine(TEST_DATABASE_URL)
TestingSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@pytest.fixture(scope="session", autouse=True)
def _schema():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)


@pytest.fixture()
def db():
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())
    session = TestingSession()
    yield session
    session.close()


@pytest.fixture()
def client(db):
    def override():
        try:
            yield db
        except Exception:
            db.rollback()
            raise

    app.dependency_overrides[get_db] = override
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture()
def refs(db):
    """Committed reference data: one fund/dept, cash+expense accounts, an open
    period and a 10_000-cent budget."""
    fund = Fund(code="GEN", name="General Fund")
    dept = Department(code="PARKS", name="Parks")
    cash = Account(code="1001", name="Cash", account_type="asset")
    expense = Account(code="5001", name="Supplies", account_type="expense")
    period = FiscalPeriod(year=2026, period=3, status="open")
    db.add_all([fund, dept, cash, expense, period])
    db.flush()
    budget = Budget(
        year=2026,
        fund_id=fund.id,
        department_id=dept.id,
        account_id=expense.id,
        amount_cents=10_000,
    )
    db.add(budget)
    db.commit()
    return SimpleNamespace(
        fund=fund, dept=dept, cash=cash, expense=expense, period=period, budget=budget
    )


def headers(role: str, name: str = "tester") -> dict:
    return {"X-User-Name": name, "X-User-Role": role}


CLERK = headers("clerk")
APPROVER = headers("approver")
MANAGER = headers("finance_manager", name="cfo")
AUDITOR = headers("auditor", name="aud")


def journal_payload(refs, key="k-1", amount=500, period_id=None, **kw):
    return {
        "idempotency_key": key,
        "period_id": period_id if period_id is not None else refs.period.id,
        "memo": "test",
        "lines": [
            {
                "fund_id": refs.fund.id,
                "department_id": refs.dept.id,
                "account_id": refs.expense.id,
                "debit_cents": amount,
            },
            {
                "fund_id": refs.fund.id,
                "department_id": refs.dept.id,
                "account_id": refs.cash.id,
                "credit_cents": amount,
            },
        ],
        **kw,
    }
