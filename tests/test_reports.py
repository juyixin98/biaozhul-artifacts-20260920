"""Reports, exports, traceability, and the auditor read-only role."""
from conftest import ACCOUNTANT, AUDITOR, OFFICER, make_payload


def _approve_and_settle(client, budget_id, amount, key):
    r = client.post(
        "/expense-requests",
        json={"budget_id": budget_id, "amount_cents": amount, "purpose": "p"},
        headers=ACCOUNTANT,
    )
    rid = r.json()["id"]
    client.post(f"/expense-requests/{rid}/approve", headers=OFFICER)
    client.post(
        "/journals",
        json=make_payload(key=key, amount=amount, expense_request_id=rid),
        headers=ACCOUNTANT,
    )
    return rid


def test_budget_report_shows_budget_encumbered_actual_available(client, seed):
    _approve_and_settle(client, seed.budget.id, 250_00, "rep-1")
    r = client.post(
        "/expense-requests",
        json={"budget_id": seed.budget.id, "amount_cents": 150_00, "purpose": "p2"},
        headers=ACCOUNTANT,
    )
    client.post(f"/expense-requests/{r.json()['id']}/approve", headers=OFFICER)

    report = client.get("/reports/budget", params={"year": 2026}, headers=AUDITOR).json()
    row = report["rows"][0]
    assert row["budget_cents"] == 1_000_00
    assert row["encumbered_cents"] == 150_00
    assert row["actual_cents"] == 250_00
    assert row["available_cents"] == 600_00


def test_budget_export_csv(client, seed):
    _approve_and_settle(client, seed.budget.id, 250_00, "rep-2")
    r = client.get("/reports/budget/export", params={"year": 2026}, headers=AUDITOR)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    lines = r.text.strip().splitlines()
    assert lines[0] == (
        "year,fund_code,department_code,account_code,"
        "budget_cents,encumbered_cents,actual_cents,available_cents"
    )
    assert lines[1] == "2026,GEN,FIN,5000,100000,0,25000,75000"


def test_traceability_from_request_to_voucher(client, seed):
    rid = _approve_and_settle(client, seed.budget.id, 250_00, "rep-3")
    req = client.get(f"/expense-requests/{rid}", headers=AUDITOR).json()
    assert req["journal_entry_id"] is not None
    entry = client.get(f"/journals/{req['journal_entry_id']}", headers=AUDITOR).json()
    assert entry["expense_request_id"] == rid
    assert entry["total_debit_cents"] == 250_00
    assert {l["account_code"] for l in entry["lines"]} == {"5000", "1000"}


def test_auditor_is_read_only(client, seed):
    assert client.get("/journals", params={"year": 2026, "month": 9}, headers=AUDITOR).status_code == 200
    assert client.get("/budgets", params={"year": 2026}, headers=AUDITOR).status_code == 200
    assert client.get("/reports/budget", params={"year": 2026}, headers=AUDITOR).status_code == 200
    assert client.get("/periods", headers=AUDITOR).status_code == 200

    assert client.post("/journals", json=make_payload(), headers=AUDITOR).status_code == 403
    assert (
        client.post(
            "/expense-requests",
            json={"budget_id": seed.budget.id, "amount_cents": 1, "purpose": "x"},
            headers=AUDITOR,
        ).status_code
        == 403
    )
    assert client.post("/periods/2026/9/close", headers=AUDITOR).status_code == 403
    assert (
        client.post(
            "/budgets",
            json={
                "year": 2026,
                "fund_code": "GEN",
                "department_code": "FIN",
                "account_code": "6000",
                "amount_cents": 100,
            },
            headers=AUDITOR,
        ).status_code
        == 403
    )
