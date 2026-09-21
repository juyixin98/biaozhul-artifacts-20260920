"""Journal posting: balance validation, references, idempotency, immutability,
reversals, and amount boundaries."""
from conftest import ACCOUNTANT, OFFICER, make_payload

from app.schemas import MAX_CENTS


def test_balanced_entry_posts(client, seed):
    r = client.post("/journals", json=make_payload(), headers=ACCOUNTANT)
    assert r.status_code == 201
    body = r.json()
    assert body["voucher_no"]
    assert len(body["lines"]) == 2
    assert body["total_debit_cents"] == 500_00
    assert body["total_debit_cents"] == body["total_credit_cents"]


def test_unbalanced_entry_rejected(client, seed):
    payload = make_payload()
    payload["lines"][1]["credit_cents"] = 1
    r = client.post("/journals", json=payload, headers=ACCOUNTANT)
    assert r.status_code == 422
    assert "not balanced" in r.json()["detail"]


def test_line_with_both_sides_filled_rejected(client, seed):
    payload = make_payload()
    payload["lines"][0]["credit_cents"] = 5
    r = client.post("/journals", json=payload, headers=ACCOUNTANT)
    assert r.status_code == 422


def test_zero_amount_line_rejected(client, seed):
    payload = make_payload()
    payload["lines"][0]["debit_cents"] = 0
    r = client.post("/journals", json=payload, headers=ACCOUNTANT)
    assert r.status_code == 422


def test_negative_amount_rejected(client, seed):
    payload = make_payload()
    payload["lines"][0]["debit_cents"] = -100
    r = client.post("/journals", json=payload, headers=ACCOUNTANT)
    assert r.status_code == 422


def test_unknown_account_rejected(client, seed):
    payload = make_payload(cr_acct="9999")
    r = client.post("/journals", json=payload, headers=ACCOUNTANT)
    assert r.status_code == 422
    assert "unknown account" in r.json()["detail"]


def test_unknown_period_rejected(client, seed):
    r = client.post("/journals", json=make_payload(year=2031), headers=ACCOUNTANT)
    assert r.status_code == 404


def test_idempotent_retry_returns_original(client, seed):
    r1 = client.post("/journals", json=make_payload(key="dup-1"), headers=ACCOUNTANT)
    assert r1.status_code == 201
    r2 = client.post("/journals", json=make_payload(key="dup-1"), headers=ACCOUNTANT)
    assert r2.status_code == 200
    assert r1.json()["id"] == r2.json()["id"]
    entries = client.get("/journals", params={"year": 2026, "month": 9}, headers=ACCOUNTANT)
    assert len(entries.json()) == 1


def test_idempotency_key_with_different_content_conflicts(client, seed):
    client.post("/journals", json=make_payload(key="c1", amount=100), headers=ACCOUNTANT)
    r = client.post("/journals", json=make_payload(key="c1", amount=200), headers=ACCOUNTANT)
    assert r.status_code == 409


def test_posted_entry_cannot_be_edited_or_deleted(client, seed):
    entry_id = client.post("/journals", json=make_payload(), headers=ACCOUNTANT).json()["id"]
    assert client.put(f"/journals/{entry_id}", json={}, headers=ACCOUNTANT).status_code == 405
    assert client.delete(f"/journals/{entry_id}", headers=ACCOUNTANT).status_code == 405


def test_reversal_creates_linked_swapped_entry(client, seed):
    entry_id = client.post("/journals", json=make_payload(), headers=ACCOUNTANT).json()["id"]
    r = client.post(
        f"/journals/{entry_id}/reverse",
        json={"year": 2026, "month": 10},
        headers=ACCOUNTANT,
    )
    assert r.status_code == 201
    body = r.json()
    assert body["reversal_of_id"] == entry_id
    original = client.get(f"/journals/{entry_id}", headers=ACCOUNTANT).json()
    for orig_line, rev_line in zip(original["lines"], body["lines"]):
        assert orig_line["debit_cents"] == rev_line["credit_cents"]
        assert orig_line["credit_cents"] == rev_line["debit_cents"]


def test_duplicate_reversal_rejected(client, seed):
    entry_id = client.post("/journals", json=make_payload(), headers=ACCOUNTANT).json()["id"]
    client.post(f"/journals/{entry_id}/reverse", json={"year": 2026, "month": 10}, headers=ACCOUNTANT)
    r = client.post(
        f"/journals/{entry_id}/reverse", json={"year": 2026, "month": 10}, headers=ACCOUNTANT
    )
    assert r.status_code == 409


def test_reversal_into_closed_period_rejected(client, seed):
    entry_id = client.post("/journals", json=make_payload(), headers=ACCOUNTANT).json()["id"]
    client.post("/periods/2026/10/close", headers=OFFICER)
    r = client.post(
        f"/journals/{entry_id}/reverse", json={"year": 2026, "month": 10}, headers=ACCOUNTANT
    )
    assert r.status_code == 409


def test_amount_boundary_max_ok_and_above_max_rejected(client, seed):
    r = client.post(
        "/journals", json=make_payload(key="max", amount=MAX_CENTS), headers=ACCOUNTANT
    )
    assert r.status_code == 201
    assert r.json()["total_debit_cents"] == MAX_CENTS

    r = client.post(
        "/journals", json=make_payload(key="max+1", amount=MAX_CENTS + 1), headers=ACCOUNTANT
    )
    assert r.status_code == 422
