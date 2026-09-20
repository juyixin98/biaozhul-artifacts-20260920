from __future__ import annotations

import os

import pytest

# Point the app at a dedicated test database before importing app modules.
_TEST_DSN = os.getenv(
    "CIVICLEDGER_TEST_DATABASE_URL",
    "postgresql+psycopg://civicledger:civicledger@localhost:5432/civicledger_test",
)
os.environ["CIVICLEDGER_DATABASE_URL"] = _TEST_DSN

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.database import Base, engine  # noqa: E402
from app.main import app  # noqa: E402

LEAD_KEY = "dev-key-lead"
ACCOUNTANT_KEY = "dev-key-accountant"
AUDITOR_KEY = "dev-key-auditor"


def _ensure_database() -> None:
    admin_url = os.getenv(
        "CIVICLEDGER_ADMIN_DATABASE_URL",
        "postgresql+psycopg://civicledger:civicledger@localhost:5432/postgres",
    )
    from sqlalchemy import create_engine

    target_db = _TEST_DSN.rsplit("/", 1)[-1]
    eng = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with eng.connect() as conn:
        exists = conn.scalar(
            text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": target_db}
        )
        if not exists:
            conn.execute(text(f'CREATE DATABASE "{target_db}"'))
    eng.dispose()


@pytest.fixture(scope="session", autouse=True)
def _database():
    _ensure_database()
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    engine.dispose()


@pytest.fixture(autouse=True)
def _clean_tables(_database):
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE TABLE idempotent_ops, journal_lines, budget_reservations, journal_entries, budgets, period_events, periods, accounts, departments, funds RESTART IDENTITY CASCADE"))
    yield


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def auth(key: str) -> dict:
    return {"X-API-Key": key}


@pytest.fixture
def base_world(client):
    """Funds, departments, accounts, an open period and a small budget."""
    def put(path, body):
        r = client.put(path, json=body, headers=auth(LEAD_KEY))
        assert r.status_code in (200, 201), r.text

    put("/funds/GF", {"code": "GF", "name": "General Fund"})
    put("/funds/SF", {"code": "SF", "name": "Special Fund"})
    put("/departments/ADMIN", {"code": "ADMIN", "name": "Admin"})
    put("/departments/PARKS", {"code": "PARKS", "name": "Parks"})
    put("/accounts/1010", {"code": "1010", "name": "Cash", "account_class": 1, "normal_side": "D"})
    put("/accounts/2010", {"code": "2010", "name": "Payables", "account_class": 2, "normal_side": "C"})
    put("/accounts/5100", {"code": "5100", "name": "Supplies", "account_class": 5, "normal_side": "D"})
    r = client.post(
        "/periods",
        json={"code": "2026-09", "start_date": "2026-09-01", "end_date": "2026-09-30"},
        headers=auth(LEAD_KEY),
    )
    assert r.status_code == 201, r.text
    r = client.put(
        "/budgets",
        json={
            "year": 2026,
            "fund_code": "GF",
            "department_code": "ADMIN",
            "account_code": "5100",
            "amount_cents": 10_000_00,
        },
        headers=auth(LEAD_KEY),
    )
    assert r.status_code == 200, r.text
    return {}


def balanced_entry(voucher_no="JV-1", amount=500_00, reservation=None):
    lines = [
        {
            "line_no": 1,
            "fund_code": "GF",
            "department_code": "ADMIN",
            "account_code": "5100",
            "debit_cents": amount,
            "credit_cents": 0,
            "reservation_request_no": reservation,
            "description": "expense",
        },
        {
            "line_no": 2,
            "fund_code": "GF",
            "department_code": "ADMIN",
            "account_code": "1010",
            "debit_cents": 0,
            "credit_cents": amount,
            "reservation_request_no": None,
            "description": "cash",
        },
    ]
    return {
        "voucher_no": voucher_no,
        "entry_date": "2026-09-10",
        "period_code": "2026-09",
        "description": "test",
        "lines": lines,
    }
