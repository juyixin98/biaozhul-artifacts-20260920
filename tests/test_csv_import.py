"""CSV import: grouping by voucher, row limit, whole-batch rollback, replay."""
from conftest import ACCOUNTANT

HEADER = "voucher_no,fund_code,department_code,account_code,debit_cents,credit_cents,memo"


def _csv(rows):
    return HEADER + "\n" + "\n".join(rows)


def test_import_success_groups_by_voucher(client, seed):
    csv_text = _csv(
        [
            "V1,GEN,FIN,5000,10000,0,supplies",
            "V1,GEN,FIN,1000,0,10000,",
            "V2,GEN,PRK,6000,25000,0,mower",
            "V2,GEN,PRK,1000,0,25000,",
        ]
    )
    r = client.post(
        "/journals/import",
        params={"year": 2026, "month": 9},
        content=csv_text,
        headers={**ACCOUNTANT, "Content-Type": "text/plain"},
    )
    assert r.status_code == 201
    assert r.json() == {"created": ["V1", "V2"], "reused": []}
    entries = client.get(
        "/journals", params={"year": 2026, "month": 9}, headers=ACCOUNTANT
    ).json()
    assert {e["voucher_no"] for e in entries} == {"V1", "V2"}


def test_import_rolls_back_entire_batch_on_any_error(client, seed):
    csv_text = _csv(
        [
            "V1,GEN,FIN,5000,10000,0,ok voucher",
            "V1,GEN,FIN,1000,0,10000,",
            "V2,GEN,FIN,5000,30000,0,unbalanced voucher",
            "V2,GEN,FIN,1000,0,9999,",
        ]
    )
    r = client.post(
        "/journals/import",
        params={"year": 2026, "month": 9},
        content=csv_text,
        headers={**ACCOUNTANT, "Content-Type": "text/plain"},
    )
    assert r.status_code == 422
    # The valid voucher V1 must not survive either: the batch is atomic.
    entries = client.get(
        "/journals", params={"year": 2026, "month": 9}, headers=ACCOUNTANT
    ).json()
    assert entries == []


def test_import_rolls_back_on_invalid_reference(client, seed):
    csv_text = _csv(
        [
            "V1,GEN,FIN,5000,10000,0,",
            "V1,GEN,FIN,1000,0,10000,",
            "V2,GEN,FIN,9999,5000,0,bad account",
            "V2,GEN,FIN,1000,0,5000,",
        ]
    )
    r = client.post(
        "/journals/import",
        params={"year": 2026, "month": 9},
        content=csv_text,
        headers={**ACCOUNTANT, "Content-Type": "text/plain"},
    )
    assert r.status_code == 422
    assert client.get(
        "/journals", params={"year": 2026, "month": 9}, headers=ACCOUNTANT
    ).json() == []


def test_import_row_limit(client, seed):
    rows = [f"V{i},GEN,FIN,5000,100,0," for i in range(2001)]
    r = client.post(
        "/journals/import",
        params={"year": 2026, "month": 9},
        content=_csv(rows),
        headers={**ACCOUNTANT, "Content-Type": "text/plain"},
    )
    assert r.status_code == 422
    assert "2000" in r.json()["detail"]


def test_import_replay_is_idempotent(client, seed):
    csv_text = _csv(
        [
            "V1,GEN,FIN,5000,10000,0,",
            "V1,GEN,FIN,1000,0,10000,",
        ]
    )
    kwargs = dict(
        params={"year": 2026, "month": 9},
        content=csv_text,
        headers={**ACCOUNTANT, "Content-Type": "text/plain"},
    )
    assert client.post("/journals/import", **kwargs).json()["created"] == ["V1"]
    r = client.post("/journals/import", **kwargs)
    assert r.json() == {"created": [], "reused": ["V1"]}
    entries = client.get(
        "/journals", params={"year": 2026, "month": 9}, headers=ACCOUNTANT
    ).json()
    assert len(entries) == 1
