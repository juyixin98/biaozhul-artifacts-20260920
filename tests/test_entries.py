from __future__ import annotations

from app.config import settings
from tests.conftest import ACCOUNTANT_KEY, AUDITOR_KEY, LEAD_KEY, auth, balanced_entry


def test_post_balanced_entry_ok(client, base_world):
    r = client.post("/entries", json=balanced_entry(), headers=auth(ACCOUNTANT_KEY))
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["voucher_no"] == "JV-1"
    assert sum(l["debit_cents"] for l in body["lines"]) == 500_00


def test_unbalanced_entry_rejected(client, base_world):
    payload = balanced_entry()
    payload["lines"][1]["credit_cents"] = 499_99
    r = client.post("/entries", json=payload, headers=auth(ACCOUNTANT_KEY))
    assert r.status_code == 422
    assert "not balanced" in r.json()["error"]["message"]
    # nothing posted
    r = client.get("/entries?period_code=2026-09", headers=auth(AUDITOR_KEY))
    assert r.json() == []


def test_line_cannot_have_both_sides(client, base_world):
    payload = balanced_entry()
    payload["lines"][0]["credit_cents"] = 10
    r = client.post("/entries", json=payload, headers=auth(ACCOUNTANT_KEY))
    assert r.status_code == 422


def test_amount_boundary_enforced(client, base_world):
    too_big = settings.max_amount_cents + 1
    payload = balanced_entry(amount=too_big)
    r = client.post("/entries", json=payload, headers=auth(ACCOUNTANT_KEY))
    assert r.status_code == 422
    assert "maximum" in r.json()["error"]["message"]


def test_max_amount_accepted_within_budget(client, base_world):
    # The application ceiling is the enforced boundary; give the budget enough
    # room so that ceiling, not budget availability, is what is tested.
    r = client.put(
        "/budgets",
        json={
            "year": 2026,
            "fund_code": "GF",
            "department_code": "ADMIN",
            "account_code": "5100",
            "amount_cents": settings.max_amount_cents,
        },
        headers=auth(LEAD_KEY),
    )
    assert r.status_code == 200, r.text
    payload = balanced_entry(amount=settings.max_amount_cents)
    r = client.post("/entries", json=payload, headers=auth(ACCOUNTANT_KEY))
    assert r.status_code == 201, r.text


def test_posted_entry_cannot_be_edited(client, base_world):
    r = client.post("/entries", json=balanced_entry(), headers=auth(ACCOUNTANT_KEY))
    assert r.status_code == 201
    # There is deliberately no edit/delete endpoint; a PUT/PATCH/DELETE must 405.
    entry_id = r.json()["id"]
    for method in ("put", "patch", "delete"):
        resp = getattr(client, method)(
            f"/entries/{entry_id}", headers=auth(LEAD_KEY)
        )
        assert resp.status_code in (404, 405)
