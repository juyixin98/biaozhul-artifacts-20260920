import os
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base, get_db
from app.main import app
from app.models import Account, Budget, Department, Fund, Period

ACCOUNTANT = {"X-User-Id": "amy", "X-Role": "accountant"}
OFFICER = {"X-User-Id": "olivia", "X-Role": "finance_officer"}
AUDITOR = {"X-User-Id": "andy", "X-Role": "auditor"}


def make_payload(
    year=2026,
    month=9,
    key="key-1",
    amount=500_00,
    fund="GEN",
    dept="FIN",
    dr_acct="5000",
    cr_acct="1000",
    **extra,
):
    payload = {
        "year": year,
        "month": month,
        "idempotency_key": key,
        "lines": [
            {
                "fund_code": fund,
                "department_code": dept,
                "account_code": dr_acct,
                "debit_cents": amount,
                "credit_cents": 0,
            },
            {
                "fund_code": fund,
                "department_code": dept,
                "account_code": cr_acct,
                "debit_cents": 0,
                "credit_cents": amount,
            },
        ],
    }
    payload.update(extra)
    return payload


@pytest.fixture()
def engine(tmp_path):
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        eng = create_engine(url, pool_pre_ping=True)
    else:
        eng = create_engine(
            f"sqlite:///{tmp_path}/test.db",
            connect_args={"check_same_thread": False, "timeout": 30},
        )
    Base.metadata.create_all(eng)
    yield eng
    Base.metadata.drop_all(eng)
    eng.dispose()


@pytest.fixture()
def Session(engine):
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@pytest.fixture()
def db(Session):
    session = Session()
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
def seed(db):
    """Reference data: GEN/CAP funds, FIN/PRK departments, 1000/5000/6000
    accounts, open periods 2026-09 and 2026-10, and one budget line
    GEN/FIN/5000 of 1000.00 (1000_00 cents)."""
    funds = [Fund(code="GEN", name="General Fund"), Fund(code="CAP", name="Capital Projects")]
    depts = [Department(code="FIN", name="Finance"), Department(code="PRK", name="Parks")]
    accts = [
        Account(code="1000", name="Cash"),
        Account(code="5000", name="Supplies Expense"),
        Account(code="6000", name="Equipment Expense"),
    ]
    db.add_all(funds + depts + accts)
    db.flush()
    db.add_all([Period(year=2026, month=9), Period(year=2026, month=10)])
    budget = Budget(
        year=2026,
        fund_id=funds[0].id,
        department_id=depts[0].id,
        account_id=accts[1].id,
        amount_cents=1_000_00,
    )
    db.add(budget)
    db.commit()
    return SimpleNamespace(
        funds={f.code: f for f in funds},
        departments={d.code: d for d in depts},
        accounts={a.code: a for a in accts},
        budget=budget,
    )
