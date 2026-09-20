from .conftest import APPROVER, AUDITOR, CLERK, journal_payload


def test_budget_report_shows_all_figures(client, refs):
    client.post(
        "/encumbrances",
        json={
            "year": 2026,
            "fund_id": refs.fund.id,
            "department_id": refs.dept.id,
            "account_id": refs.expense.id,
            "amount_cents": 2_000,
        },
        headers=APPROVER,
    )
    client.post("/journals", json=journal_payload(refs, key="rep-1", amount=1_000), headers=CLERK)

    r = client.get("/reports/budget?year=2026", headers=AUDITOR)
    assert r.status_code == 200
    row = r.json()["rows"][0]
    assert row["budget_cents"] == 10_000
    assert row["encumbered_cents"] == 2_000
    assert row["actual_cents"] == 1_000
    assert row["available_cents"] == 7_000


def test_budget_export_csv(client, refs):
    r = client.get("/reports/budget/export?year=2026", headers=AUDITOR)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    lines = r.text.strip().splitlines()
    assert lines[0].startswith("year,fund_code")
    assert "GEN,PARKS,5001" in lines[1]


def test_budget_activity_traces_to_source_documents(client, refs):
    enc = client.post(
        "/encumbrances",
        json={
            "year": 2026,
            "fund_id": refs.fund.id,
            "department_id": refs.dept.id,
            "account_id": refs.expense.id,
            "amount_cents": 1_500,
        },
        headers=APPROVER,
    ).json()
    entry = client.post(
        "/journals",
        json=journal_payload(refs, key="trace-1", amount=1_500, encumbrance_id=enc["id"]),
        headers=CLERK,
    ).json()

    r = client.get(f"/budgets/{refs.budget.id}/activity", headers=AUDITOR)
    assert r.status_code == 200
    body = r.json()
    assert body["encumbrances"][0]["journal_entry_id"] == entry["id"]
    assert any(l["journal_entry_id"] == entry["id"] for l in body["journal_lines"])


def test_auditor_is_read_only(client, refs):
    # reads work
    assert client.get("/journals", headers=AUDITOR).status_code == 200
    assert client.get("/budgets", headers=AUDITOR).status_code == 200
    assert client.get("/periods", headers=AUDITOR).status_code == 200
    assert client.get("/reports/budget?year=2026", headers=AUDITOR).status_code == 200
    # writes are forbidden
    assert client.post("/journals", json=journal_payload(refs), headers=AUDITOR).status_code == 403
    assert client.post(
        "/encumbrances",
        json={"year": 2026, "fund_id": 1, "department_id": 1, "account_id": 1, "amount_cents": 1},
        headers=AUDITOR,
    ).status_code == 403
    assert client.post("/budgets", json={}, headers=AUDITOR).status_code == 403
    assert client.post(f"/periods/{refs.period.id}/close", headers=AUDITOR).status_code == 403
