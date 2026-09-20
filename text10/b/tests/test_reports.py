"""报表与审计日志查询。"""

from conftest import ACCOUNTANT, AUDITOR, MANAGER, balanced_body


def test_budget_report_shows_all_figures(client, budget):
    req = client.post("/requests", json={"budget_id": budget.id, "amount_cents": 200_00},
                      headers=ACCOUNTANT).json()
    client.post(f"/requests/{req['id']}/approve", headers=MANAGER)
    req2 = client.post("/requests", json={"budget_id": budget.id, "amount_cents": 100_00},
                       headers=ACCOUNTANT).json()
    client.post(f"/requests/{req2['id']}/approve", headers=MANAGER)
    client.post(f"/requests/{req2['id']}/post", headers=ACCOUNTANT)

    rows = client.get("/reports/budget", params={"year": 2026}, headers=AUDITOR).json()
    assert len(rows) == 1
    row = rows[0]
    assert row["amount_cents"] == 1000_00
    assert row["encumbered_cents"] == 200_00
    assert row["actual_cents"] == 100_00
    assert row["available_cents"] == 700_00


def test_budget_report_export_csv(client, budget):
    resp = client.get("/reports/budget/export", params={"year": 2026}, headers=AUDITOR)
    assert resp.status_code == 200
    assert "budget_cents" in resp.text
    assert "100000" in resp.text


def test_audit_log_records_actions(client, refs):
    client.post("/journals", json=balanced_body(refs),
                headers={**ACCOUNTANT, "Idempotency-Key": "al-1"})
    logs = client.get("/audit-log", headers=AUDITOR).json()
    assert any(l["action"] == "post_journal" and l["actor"] == "accountant" for l in logs)
