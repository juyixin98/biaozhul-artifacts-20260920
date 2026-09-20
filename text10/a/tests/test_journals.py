from app.models import JournalEntry
from app.schemas import MAX_CENTS

from .conftest import CLERK, MANAGER, journal_payload


def test_post_balanced_journal(client, refs):
    r = client.post("/journals", json=journal_payload(refs), headers=CLERK)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["id"] > 0
    assert len(body["lines"]) == 2
    assert body["source"] == "manual"


def test_unbalanced_journal_rejected(client, refs):
    payload = journal_payload(refs)
    payload["lines"][1]["credit_cents"] = 499
    r = client.post("/journals", json=payload, headers=CLERK)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "unbalanced-entry"


def test_invalid_reference_rejected(client, refs):
    payload = journal_payload(refs)
    payload["lines"][0]["fund_id"] = 99999
    r = client.post("/journals", json=payload, headers=CLERK)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid-reference"


def test_zero_amount_line_rejected(client, refs):
    payload = journal_payload(refs)
    payload["lines"][0]["debit_cents"] = 0
    r = client.post("/journals", json=payload, headers=CLERK)
    assert r.status_code == 422  # schema: exactly one side must be positive


def test_negative_amount_rejected(client, refs):
    payload = journal_payload(refs)
    payload["lines"][0]["debit_cents"] = -100
    r = client.post("/journals", json=payload, headers=CLERK)
    assert r.status_code == 422


def test_amount_overflow_rejected(client, refs):
    payload = journal_payload(refs, amount=MAX_CENTS + 1)
    r = client.post("/journals", json=payload, headers=CLERK)
    assert r.status_code == 422


def test_max_amount_boundary_accepted(client, refs):
    # No budget big enough, but the expense account has a budget of 10_000;
    # use the cash account on both sides is impossible (one side each), so
    # point the debit at a non-expense account to skip budget control.
    payload = journal_payload(refs, amount=MAX_CENTS)
    payload["lines"][0]["account_id"] = refs.cash.id
    payload["lines"][1]["account_id"] = refs.cash.id
    payload["lines"][0]["debit_cents"] = MAX_CENTS
    payload["lines"][1]["credit_cents"] = MAX_CENTS
    r = client.post("/journals", json=payload, headers=CLERK)
    assert r.status_code == 201, r.text


def test_idempotent_replay_returns_original(client, refs):
    payload = journal_payload(refs, key="replay-1")
    r1 = client.post("/journals", json=payload, headers=CLERK)
    r2 = client.post("/journals", json=payload, headers=CLERK)
    assert r1.status_code == 201
    assert r2.status_code == 200
    assert r1.json()["id"] == r2.json()["id"]


def test_idempotency_key_conflict(client, refs):
    r1 = client.post("/journals", json=journal_payload(refs, key="dup-1"), headers=CLERK)
    assert r1.status_code == 201
    r2 = client.post(
        "/journals", json=journal_payload(refs, key="dup-1", amount=600), headers=CLERK
    )
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "idempotency-key-conflict"


def test_posted_entry_is_immutable(client, refs):
    r = client.post("/journals", json=journal_payload(refs), headers=CLERK)
    entry_id = r.json()["id"]
    assert client.put(f"/journals/{entry_id}", json={}, headers=MANAGER).status_code == 405
    assert client.delete(f"/journals/{entry_id}", headers=MANAGER).status_code == 405


def test_reversal_mirrors_and_links(client, refs, db):
    r = client.post("/journals", json=journal_payload(refs, amount=700), headers=CLERK)
    original = r.json()
    r2 = client.post(
        f"/journals/{original['id']}/reverse",
        json={"idempotency_key": "rev-1", "period_id": refs.period.id},
        headers=CLERK,
    )
    assert r2.status_code == 201, r2.text
    reversal = r2.json()
    assert reversal["reversal_of_id"] == original["id"]
    assert reversal["source"] == "reversal"
    # mirrored amounts
    for orig_line, rev_line in zip(original["lines"], reversal["lines"]):
        assert orig_line["debit_cents"] == rev_line["credit_cents"]
        assert orig_line["credit_cents"] == rev_line["debit_cents"]
    # budget actual returned to zero
    db.expire_all()
    assert db.get(type(refs.budget), refs.budget.id).actual_cents == 0


def test_duplicate_reversal_rejected(client, refs):
    r = client.post("/journals", json=journal_payload(refs), headers=CLERK)
    entry_id = r.json()["id"]
    body = {"idempotency_key": "rev-a", "period_id": refs.period.id}
    assert client.post(f"/journals/{entry_id}/reverse", json=body, headers=CLERK).status_code == 201
    body2 = {"idempotency_key": "rev-b", "period_id": refs.period.id}
    r2 = client.post(f"/journals/{entry_id}/reverse", json=body2, headers=CLERK)
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "already-reversed"


def test_reversal_replay_is_idempotent(client, refs):
    r = client.post("/journals", json=journal_payload(refs), headers=CLERK)
    entry_id = r.json()["id"]
    body = {"idempotency_key": "rev-replay", "period_id": refs.period.id}
    r1 = client.post(f"/journals/{entry_id}/reverse", json=body, headers=CLERK)
    r2 = client.post(f"/journals/{entry_id}/reverse", json=body, headers=CLERK)
    assert r1.status_code == 201
    assert r2.status_code == 200
    assert r1.json()["id"] == r2.json()["id"]


def test_reversal_into_closed_period_rejected(client, refs):
    r = client.post("/journals", json=journal_payload(refs), headers=CLERK)
    entry_id = r.json()["id"]
    assert client.post(f"/periods/{refs.period.id}/close", headers=MANAGER).status_code == 200
    r2 = client.post(
        f"/journals/{entry_id}/reverse",
        json={"idempotency_key": "rev-closed", "period_id": refs.period.id},
        headers=CLERK,
    )
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "period-closed"


def test_post_to_closed_period_rejected(client, refs):
    assert client.post(f"/periods/{refs.period.id}/close", headers=MANAGER).status_code == 200
    r = client.post("/journals", json=journal_payload(refs), headers=CLERK)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "period-closed"


def _csv(rows):
    return "voucher_no,fund_code,department_code,account_code,debit,credit\n" + "\n".join(
        ",".join(str(c) for c in row) for row in rows
    )


def test_csv_import_ok(client, refs, db):
    content = _csv(
        [
            ("V1", "GEN", "PARKS", "5001", "10.00", ""),
            ("V1", "GEN", "PARKS", "1001", "", "10.00"),
            ("V2", "GEN", "PARKS", "5001", "0.01", ""),
            ("V2", "GEN", "PARKS", "1001", "", "0.01"),
        ]
    )
    r = client.post(
        "/journals/import?year=2026&period=3",
        files={"file": ("j.csv", content, "text/csv")},
        headers={**CLERK, "X-Idempotency-Key": "batch-1"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["imported"] == 2
    assert db.query(JournalEntry).count() == 2


def test_csv_import_replay_is_idempotent(client, refs, db):
    content = _csv([("V1", "GEN", "PARKS", "5001", "5.00", ""), ("V1", "GEN", "PARKS", "1001", "", "5.00")])
    args = dict(
        files={"file": ("j.csv", content, "text/csv")},
        headers={**CLERK, "X-Idempotency-Key": "batch-replay"},
    )
    r1 = client.post("/journals/import?year=2026&period=3", **args)
    r2 = client.post("/journals/import?year=2026&period=3", **args)
    assert r1.status_code == 201
    assert r2.status_code == 201
    assert r2.json()["entries"][0]["replayed"] is True
    assert db.query(JournalEntry).count() == 1


def test_csv_batch_rolls_back_on_any_error(client, refs, db):
    content = _csv(
        [
            ("V1", "GEN", "PARKS", "5001", "100.00", ""),
            ("V1", "GEN", "PARKS", "1001", "", "100.00"),
            ("V2", "GEN", "PARKS", "5001", "50.00", ""),  # unbalanced voucher
        ]
    )
    r = client.post(
        "/journals/import?year=2026&period=3",
        files={"file": ("j.csv", content, "text/csv")},
        headers={**CLERK, "X-Idempotency-Key": "batch-bad"},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "csv-validation-failed"
    assert db.query(JournalEntry).count() == 0  # nothing from V1 either


def test_csv_too_many_rows(client, refs):
    rows = [("V1", "GEN", "PARKS", "5001", "1.00", ""), ("V1", "GEN", "PARKS", "1001", "", "1.00")]
    # pad with extra rows of the same balanced voucher
    content = _csv(rows * 1001)  # 2002 data rows
    r = client.post(
        "/journals/import?year=2026&period=3",
        files={"file": ("j.csv", content, "text/csv")},
        headers={**CLERK, "X-Idempotency-Key": "batch-huge"},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "csv-too-many-rows"
