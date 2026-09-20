from __future__ import annotations

from tests.conftest import AUDITOR_KEY, auth, balanced_entry


def test_missing_api_key_unauthorized(client, base_world):
    r = client.post("/entries", json=balanced_entry())
    assert r.status_code == 401


def test_bad_api_key_unauthorized(client, base_world):
    r = client.post("/entries", json=balanced_entry(), headers={"X-API-Key": "nope"})
    assert r.status_code == 401


def test_auditor_is_read_only(client, base_world):
    h = auth(AUDITOR_KEY)
    # Reads allowed
    assert client.get("/entries", headers=h).status_code == 200
    assert client.get("/budgets", headers=h).status_code == 200
    assert client.get("/reports/budget-usage", headers=h).status_code == 200
    assert client.get("/periods", headers=h).status_code == 200
    # Writes forbidden
    assert client.post("/entries", json=balanced_entry(), headers=h).status_code == 403
    assert client.post(
        "/reservations",
        json={
            "request_no": "X",
            "year": 2026,
            "fund_code": "GF",
            "department_code": "ADMIN",
            "account_code": "5100",
            "amount_cents": 1,
        },
        headers=h,
    ).status_code == 403
    assert client.put(
        "/funds/XX", json={"code": "XX", "name": "x"}, headers=h
    ).status_code == 403


def test_usage_report_and_trace(client, base_world):
    client.post(
        "/entries", json=balanced_entry("JV-TRACE", amount=420_00), headers=auth("dev-key-accountant")
    )
    r = client.get(
        "/reports/budget-usage?year=2026&fund_code=GF&department_code=ADMIN&account_code=5100",
        headers=auth(AUDITOR_KEY),
    )
    assert r.status_code == 200
    row = r.json()[0]
    assert row["actual_cents"] == 420_00
    assert row["available_cents"] == 10_000_00 - 420_00

    trace = client.get(
        "/reports/budget-trace?year=2026&fund_code=GF&department_code=ADMIN&account_code=5100",
        headers=auth(AUDITOR_KEY),
    ).json()
    vouchers = {l["voucher_no"] for l in trace["lines"]}
    assert "JV-TRACE" in vouchers

    csv_resp = client.get(
        "/reports/budget-usage.csv?year=2026", headers=auth(AUDITOR_KEY)
    )
    assert csv_resp.status_code == 200
    assert "actual_cents" in csv_resp.text
