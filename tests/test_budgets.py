"""Budget encumbrance: approve/cancel/settle, overspend guard, concurrency."""
import threading

from conftest import ACCOUNTANT, OFFICER, make_payload

from app.errors import DomainError
from app.models import Budget
from app.schemas import ExpenseRequestCreate
from app.services.budgets import approve_expense_request, create_expense_request


def _create_request(client, budget_id, amount, purpose="office supplies"):
    r = client.post(
        "/expense-requests",
        json={"budget_id": budget_id, "amount_cents": amount, "purpose": purpose},
        headers=ACCOUNTANT,
    )
    assert r.status_code == 201
    return r.json()["id"]


def _budget_row(client, year=2026):
    rows = client.get("/budgets", params={"year": year}, headers=ACCOUNTANT).json()
    assert len(rows) == 1
    return rows[0]


def test_approve_encumbers_and_reduces_available(client, seed):
    rid = _create_request(client, seed.budget.id, 400_00)
    r = client.post(f"/expense-requests/{rid}/approve", headers=OFFICER)
    assert r.status_code == 200
    assert r.json()["status"] == "approved"
    row = _budget_row(client)
    assert row["encumbered_cents"] == 400_00
    assert row["actual_cents"] == 0
    assert row["available_cents"] == 600_00


def test_approve_beyond_available_rejected_and_rolls_back(client, seed):
    rid = _create_request(client, seed.budget.id, 1_500_00)
    r = client.post(f"/expense-requests/{rid}/approve", headers=OFFICER)
    assert r.status_code == 409
    # The status change is rolled back together with the failed encumbrance.
    assert client.get(f"/expense-requests/{rid}", headers=ACCOUNTANT).json()["status"] == "pending"
    assert _budget_row(client)["encumbered_cents"] == 0


def test_cancel_releases_encumbrance_exactly_once(client, seed):
    rid = _create_request(client, seed.budget.id, 400_00)
    client.post(f"/expense-requests/{rid}/approve", headers=OFFICER)
    r = client.post(f"/expense-requests/{rid}/cancel", headers=ACCOUNTANT)
    assert r.status_code == 200
    assert _budget_row(client)["encumbered_cents"] == 0
    # Second cancel: conflict, and no further release (encumbered stays 0,
    # it must not go negative).
    r = client.post(f"/expense-requests/{rid}/cancel", headers=ACCOUNTANT)
    assert r.status_code == 409
    assert _budget_row(client)["encumbered_cents"] == 0


def test_posting_settles_encumbrance_into_actual(client, seed):
    rid = _create_request(client, seed.budget.id, 400_00)
    client.post(f"/expense-requests/{rid}/approve", headers=OFFICER)
    r = client.post(
        "/journals",
        json=make_payload(key="settle-1", amount=400_00, expense_request_id=rid),
        headers=ACCOUNTANT,
    )
    assert r.status_code == 201
    assert r.json()["expense_request_id"] == rid
    row = _budget_row(client)
    assert row["encumbered_cents"] == 0
    assert row["actual_cents"] == 400_00
    assert row["available_cents"] == 600_00
    req = client.get(f"/expense-requests/{rid}", headers=ACCOUNTANT).json()
    assert req["status"] == "posted"
    assert req["journal_entry_id"] == r.json()["id"]


def test_settle_amount_mismatch_rejected(client, seed):
    rid = _create_request(client, seed.budget.id, 400_00)
    client.post(f"/expense-requests/{rid}/approve", headers=OFFICER)
    r = client.post(
        "/journals",
        json=make_payload(key="settle-2", amount=300_00, expense_request_id=rid),
        headers=ACCOUNTANT,
    )
    assert r.status_code == 422
    # Nothing changed: encumbrance intact, request still approved.
    assert _budget_row(client)["encumbered_cents"] == 400_00
    assert client.get(f"/expense-requests/{rid}", headers=ACCOUNTANT).json()["status"] == "approved"


def test_concurrent_approvals_never_exceed_budget(Session, seed):
    """10 requests of 200.00 race for a 1000.00 budget: exactly 5 may win."""
    budget_id = seed.budget.id
    request_ids = []
    setup = Session()
    for i in range(10):
        req = create_expense_request(
            setup, ExpenseRequestCreate(budget_id=budget_id, amount_cents=200_00, purpose=f"r{i}"), "amy"
        )
        request_ids.append(req.id)
    setup.close()

    barrier = threading.Barrier(len(request_ids))
    outcomes = {}

    def worker(rid):
        session = Session()
        try:
            barrier.wait(timeout=15)
            approve_expense_request(session, rid, "olivia")
            outcomes[rid] = True
        except DomainError:
            outcomes[rid] = False
        finally:
            session.close()

    threads = [threading.Thread(target=worker, args=(rid,)) for rid in request_ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert sum(outcomes.values()) == 5
    check = Session()
    budget = check.get(Budget, budget_id)
    assert budget.encumbered_cents == 5 * 200_00
    assert budget.encumbered_cents + budget.actual_cents <= budget.amount_cents
    check.close()
