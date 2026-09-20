import threading

from app.errors import DomainError
from app.models import Budget, Encumbrance
from app.services.budgets import approve_encumbrance, cancel_encumbrance

from .conftest import APPROVER, CLERK, TestingSession, journal_payload


def _enc_payload(refs, amount):
    return {
        "year": 2026,
        "fund_id": refs.fund.id,
        "department_id": refs.dept.id,
        "account_id": refs.expense.id,
        "amount_cents": amount,
        "description": "test request",
    }


def test_approve_encumbrance_reduces_available(client, refs, db):
    r = client.post("/encumbrances", json=_enc_payload(refs, 4_000), headers=APPROVER)
    assert r.status_code == 201, r.text
    db.expire_all()
    budget = db.get(Budget, refs.budget.id)
    assert budget.encumbered_cents == 4_000
    assert budget.amount_cents - budget.encumbered_cents - budget.actual_cents == 6_000


def test_approve_beyond_available_rejected(client, refs):
    r = client.post("/encumbrances", json=_enc_payload(refs, 10_001), headers=APPROVER)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "budget-exceeded"


def test_concurrent_approvals_never_overspend(refs):
    """Budget is 10_000; two 7_000 approvals race — exactly one may win."""
    results = {}
    barrier = threading.Barrier(2)

    def worker(name):
        session = TestingSession()
        try:
            barrier.wait(timeout=10)
            approve_encumbrance(
                session,
                year=2026,
                fund_id=refs.fund.id,
                department_id=refs.dept.id,
                account_id=refs.expense.id,
                amount_cents=7_000,
                description=name,
                actor=name,
            )
            session.commit()
            results[name] = "ok"
        except DomainError as e:
            session.rollback()
            results[name] = e.code
        finally:
            session.close()

    threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert sorted(results.values()) == ["budget-exceeded", "ok"]
    check = TestingSession()
    budget = check.get(Budget, refs.budget.id)
    assert budget.encumbered_cents == 7_000
    assert check.query(Encumbrance).filter_by(status="open").count() == 1
    check.close()


def test_cancel_releases_exactly_once(client, refs, db):
    r = client.post("/encumbrances", json=_enc_payload(refs, 3_000), headers=APPROVER)
    enc_id = r.json()["id"]
    assert client.post(f"/encumbrances/{enc_id}/cancel", headers=APPROVER).status_code == 200
    r2 = client.post(f"/encumbrances/{enc_id}/cancel", headers=APPROVER)
    assert r2.status_code == 409
    db.expire_all()
    assert db.get(Budget, refs.budget.id).encumbered_cents == 0


def test_liquidation_releases_encumbrance_and_records_actual(client, refs, db):
    r = client.post("/encumbrances", json=_enc_payload(refs, 5_000), headers=APPROVER)
    enc_id = r.json()["id"]

    payload = journal_payload(refs, key="liq-1", amount=5_000, encumbrance_id=enc_id)
    r2 = client.post("/journals", json=payload, headers=CLERK)
    assert r2.status_code == 201, r2.text

    db.expire_all()
    budget = db.get(Budget, refs.budget.id)
    assert budget.encumbered_cents == 0
    assert budget.actual_cents == 5_000
    # available unchanged by liquidation: 10_000 - 0 - 5_000
    assert budget.amount_cents - budget.encumbered_cents - budget.actual_cents == 5_000
    enc = db.get(Encumbrance, enc_id)
    assert enc.status == "liquidated"
    assert enc.journal_entry_id == r2.json()["id"]


def test_liquidation_amount_must_match(client, refs):
    r = client.post("/encumbrances", json=_enc_payload(refs, 5_000), headers=APPROVER)
    enc_id = r.json()["id"]
    payload = journal_payload(refs, key="liq-bad", amount=4_000, encumbrance_id=enc_id)
    r2 = client.post("/journals", json=payload, headers=CLERK)
    assert r2.status_code == 422
    assert r2.json()["error"]["code"] == "encumbrance-amount-mismatch"


def test_cannot_liquidate_twice(client, refs):
    r = client.post("/encumbrances", json=_enc_payload(refs, 5_000), headers=APPROVER)
    enc_id = r.json()["id"]
    p1 = journal_payload(refs, key="liq-x1", amount=5_000, encumbrance_id=enc_id)
    assert client.post("/journals", json=p1, headers=CLERK).status_code == 201
    p2 = journal_payload(refs, key="liq-x2", amount=5_000, encumbrance_id=enc_id)
    r2 = client.post("/journals", json=p2, headers=CLERK)
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "encumbrance-not-open"


def test_unencumbered_spend_cannot_make_available_negative(client, refs):
    # spend 8_000 directly, then try 3_000 more against the 10_000 budget
    p1 = journal_payload(refs, key="spend-1", amount=8_000)
    assert client.post("/journals", json=p1, headers=CLERK).status_code == 201
    p2 = journal_payload(refs, key="spend-2", amount=3_000)
    r = client.post("/journals", json=p2, headers=CLERK)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "budget-exceeded"


def test_cancel_then_reapprove_same_budget(client, refs, db):
    r = client.post("/encumbrances", json=_enc_payload(refs, 9_000), headers=APPROVER)
    enc_id = r.json()["id"]
    assert client.post(f"/encumbrances/{enc_id}/cancel", headers=APPROVER).status_code == 200
    # full amount is available again
    r2 = client.post("/encumbrances", json=_enc_payload(refs, 10_000), headers=APPROVER)
    assert r2.status_code == 201
