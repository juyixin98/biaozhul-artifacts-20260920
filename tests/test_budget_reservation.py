from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from app.database import SessionLocal
from app.errors import ConflictError
from app.schemas import ReservationCancelIn, ReservationIn
from app.services.budgets import approve_reservation, cancel_reservation
from tests.conftest import (
    ACCOUNTANT_KEY,
    AUDITOR_KEY,
    LEAD_KEY,
    auth,
    balanced_entry,
)


def _reservation(no, amount):
    return {
        "request_no": no,
        "year": 2026,
        "fund_code": "GF",
        "department_code": "ADMIN",
        "account_code": "5100",
        "amount_cents": amount,
        "description": "x",
    }


def test_approve_reservation_preoccupies(client, base_world):
    r = client.post("/reservations", json=_reservation("REQ-1", 4_000_00), headers=auth(LEAD_KEY))
    assert r.status_code == 201, r.text
    usage = client.get(
        "/reports/budget-usage?year=2026&fund_code=GF&department_code=ADMIN&account_code=5100",
        headers=auth(AUDITOR_KEY),
    ).json()[0]
    assert usage["reserved_cents"] == 4_000_00
    assert usage["available_cents"] == 6_000_00


def test_approve_over_budget_rejected(client, base_world):
    r = client.post("/reservations", json=_reservation("REQ-2", 11_000_00), headers=auth(LEAD_KEY))
    assert r.status_code == 409
    assert "Budget exceeded" in r.json()["error"]["message"]


def test_accountant_cannot_approve(client, base_world):
    r = client.post(
        "/reservations", json=_reservation("REQ-3", 100), headers=auth(ACCOUNTANT_KEY)
    )
    assert r.status_code == 403


def test_concurrent_approvals_cannot_oversubscribe(base_world):
    """Two simultaneous approvals whose total exceeds the budget: exactly one wins."""

    def attempt(no):
        db = SessionLocal()
        try:
            approve_reservation(
                db, ReservationIn(**_reservation(no, 6_000_00)), actor="lead"
            )
            db.commit()
            return "ok"
        except ConflictError:
            db.rollback()
            return "rejected"
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, ["REQ-C1", "REQ-C2"]))
    assert sorted(results) == ["ok", "rejected"]

    db = SessionLocal()
    try:
        from app.models import Budget

        budget = db.query(Budget).filter_by(
            year=2026, fund_code="GF", department_code="ADMIN", account_code="5100"
        ).one()
        assert budget.reserved_cents == 6_000_00
        assert budget.actual_cents == 0
        assert budget.reserved_cents + budget.actual_cents <= budget.amount_cents
    finally:
        db.close()


def test_posting_consumes_reservation_and_releases_preoccupation(client, base_world):
    r = client.post("/reservations", json=_reservation("REQ-4", 3_000_00), headers=auth(LEAD_KEY))
    assert r.status_code == 201
    r = client.post(
        "/entries",
        json=balanced_entry("JV-POST", amount=3_000_00, reservation="REQ-4"),
        headers=auth(ACCOUNTANT_KEY),
    )
    assert r.status_code == 201, r.text
    usage = client.get(
        "/reports/budget-usage?year=2026&fund_code=GF&department_code=ADMIN&account_code=5100",
        headers=auth(AUDITOR_KEY),
    ).json()[0]
    assert usage["reserved_cents"] == 0
    assert usage["actual_cents"] == 3_000_00
    reservation = client.get(
        "/reservations?request_no=REQ-4", headers=auth(AUDITOR_KEY)
    ).json()[0]
    assert reservation["status"] == "consumed"
    assert reservation["consumed_voucher_no"] == "JV-POST"


def test_posting_reservation_amount_mismatch_rejected(client, base_world):
    client.post("/reservations", json=_reservation("REQ-5", 3_000_00), headers=auth(LEAD_KEY))
    r = client.post(
        "/entries",
        json=balanced_entry("JV-MM", amount=2_999_99, reservation="REQ-5"),
        headers=auth(ACCOUNTANT_KEY),
    )
    assert r.status_code == 422


def test_cancel_releases_once(client, base_world):
    client.post("/reservations", json=_reservation("REQ-6", 1_000_00), headers=auth(LEAD_KEY))
    body = ReservationCancelIn(reason="not needed").model_dump()
    r1 = client.post(
        "/reservations/REQ-6/cancel", json=body, headers=auth(ACCOUNTANT_KEY)
    )
    assert r1.status_code == 200, r1.text
    r2 = client.post(
        "/reservations/REQ-6/cancel", json=body, headers=auth(ACCOUNTANT_KEY)
    )
    assert r2.status_code == 409
    usage = client.get(
        "/reports/budget-usage?year=2026&fund_code=GF&department_code=ADMIN&account_code=5100",
        headers=auth(AUDITOR_KEY),
    ).json()[0]
    assert usage["reserved_cents"] == 0
    assert usage["available_cents"] == 10_000_00


def test_concurrent_cancel_and_consume(client, base_world):
    """Cancel and posting-against the same reservation race: one outcome wins,
    counters stay invariant either way."""
    client.post("/reservations", json=_reservation("REQ-7", 2_000_00), headers=auth(LEAD_KEY))

    def consume():
        db = SessionLocal()
        try:
            from app.schemas import EntryIn
            from app.services.entries import create_entry

            payload = EntryIn(**balanced_entry("JV-RACE", amount=2_000_00, reservation="REQ-7"))
            create_entry(db, payload, actor="accountant")
            db.commit()
            return "consumed"
        except Exception:
            db.rollback()
            return "lost"
        finally:
            db.close()

    def cancel():
        db = SessionLocal()
        try:
            cancel_reservation(
                db, "REQ-7", ReservationCancelIn(reason="x"), actor="accountant"
            )
            db.commit()
            return "cancelled"
        except Exception:
            db.rollback()
            return "lost"
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(consume)
        f2 = pool.submit(cancel)
        outcomes = {f1.result(), f2.result()}

    # Exactly one side wins; the other is lost. They cannot both win.
    assert outcomes in [{"consumed", "lost"}, {"cancelled", "lost"}], outcomes

    db = SessionLocal()
    try:
        from app.models import Budget, BudgetReservation

        budget = db.query(Budget).filter_by(
            year=2026, fund_code="GF", department_code="ADMIN", account_code="5100"
        ).one()
        reservation = db.query(BudgetReservation).filter_by(request_no="REQ-7").one()
        committed = budget.reserved_cents + budget.actual_cents
        assert committed <= budget.amount_cents
        if "consumed" in outcomes:
            assert reservation.status == "consumed"
            assert budget.actual_cents == 2_000_00
            assert budget.reserved_cents == 0
        else:
            assert reservation.status == "cancelled"
            assert budget.actual_cents == 0
            assert budget.reserved_cents == 0
    finally:
        db.close()


def test_actual_spend_without_reservation_cannot_go_negative(client, base_world):
    # Budget 10,000.00: first spend 9,000 then 2,000 -> second rejected.
    r = client.post(
        "/entries", json=balanced_entry("JV-A1", amount=9_000_00), headers=auth(ACCOUNTANT_KEY)
    )
    assert r.status_code == 201, r.text
    r = client.post(
        "/entries", json=balanced_entry("JV-A2", amount=2_000_00), headers=auth(ACCOUNTANT_KEY)
    )
    assert r.status_code == 409
