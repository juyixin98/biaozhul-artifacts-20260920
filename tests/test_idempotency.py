from __future__ import annotations

from tests.conftest import ACCOUNTANT_KEY, auth, balanced_entry


def test_same_key_same_body_returns_original(client, base_world):
    headers = {**auth(ACCOUNTANT_KEY), "Idempotency-Key": "key-1"}
    r1 = client.post("/entries", json=balanced_entry("JV-IDEM"), headers=headers)
    assert r1.status_code == 201, r1.text
    r2 = client.post("/entries", json=balanced_entry("JV-IDEM"), headers=headers)
    assert r2.status_code == 201
    assert r2.json()["id"] == r1.json()["id"]
    # only one voucher exists
    r = client.get("/entries?voucher_no=JV-IDEM", headers=auth(ACCOUNTANT_KEY))
    assert len(r.json()) == 1


def test_same_key_different_body_conflicts(client, base_world):
    headers = {**auth(ACCOUNTANT_KEY), "Idempotency-Key": "key-2"}
    r1 = client.post("/entries", json=balanced_entry("JV-IDEM-A"), headers=headers)
    assert r1.status_code == 201
    r2 = client.post("/entries", json=balanced_entry("JV-IDEM-B"), headers=headers)
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "idempotency_conflict"
    r = client.get("/entries?voucher_no=JV-IDEM-B", headers=auth(ACCOUNTANT_KEY))
    assert r.json() == []
