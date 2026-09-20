"""借贷校验、幂等键、冲销、金额边界。"""

from conftest import ACCOUNTANT, AUDITOR, MANAGER, balanced_body

from app.models import JournalEntry


def test_post_balanced_journal(client, refs):
    resp = client.post("/journals", json=balanced_body(refs),
                       headers={**ACCOUNTANT, "Idempotency-Key": "k-1"})
    assert resp.status_code == 201
    data = resp.json()
    assert data["entry"]["status"] == "posted"
    assert len(data["entry"]["lines"]) == 2
    assert data["idempotent_replay"] is False


def test_unbalanced_rejected(client, refs):
    body = balanced_body(refs)
    body["lines"][1]["credit_cents"] = 99_00
    resp = client.post("/journals", json=body,
                       headers={**ACCOUNTANT, "Idempotency-Key": "k-2"})
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "UNBALANCED"


def test_invalid_reference_rejected(client, refs):
    body = balanced_body(refs)
    body["lines"][0]["fund_id"] = 99999
    resp = client.post("/journals", json=body,
                       headers={**ACCOUNTANT, "Idempotency-Key": "k-3"})
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "INVALID_REF"


def test_zero_and_double_sided_lines_rejected(client, refs):
    body = balanced_body(refs)
    body["lines"][0]["debit_cents"] = 0
    body["lines"][0]["credit_cents"] = 0
    resp = client.post("/journals", json=body,
                       headers={**ACCOUNTANT, "Idempotency-Key": "k-4"})
    assert resp.status_code == 422

    body = balanced_body(refs)
    body["lines"][0]["credit_cents"] = 1
    resp = client.post("/journals", json=body,
                       headers={**ACCOUNTANT, "Idempotency-Key": "k-5"})
    assert resp.status_code == 422


def test_amount_boundaries(client, refs):
    # 超过单行上限
    body = balanced_body(refs, amount=1_000_000_000_000_000)
    resp = client.post("/journals", json=body,
                       headers={**ACCOUNTANT, "Idempotency-Key": "k-6"})
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "AMOUNT_TOO_LARGE"

    # 上限边界值可以过账
    body = balanced_body(refs, amount=999_999_999_999_999)
    resp = client.post("/journals", json=body,
                       headers={**ACCOUNTANT, "Idempotency-Key": "k-7"})
    assert resp.status_code == 201


def test_idempotency_same_content_replays(client, refs):
    headers = {**ACCOUNTANT, "Idempotency-Key": "k-8"}
    first = client.post("/journals", json=balanced_body(refs), headers=headers)
    second = client.post("/journals", json=balanced_body(refs), headers=headers)
    assert first.status_code == 201 and second.status_code == 201
    assert second.json()["idempotent_replay"] is True
    assert first.json()["entry"]["id"] == second.json()["entry"]["id"]


def test_idempotency_conflict_on_different_content(client, refs):
    headers = {**ACCOUNTANT, "Idempotency-Key": "k-9"}
    client.post("/journals", json=balanced_body(refs), headers=headers)
    resp = client.post("/journals", json=balanced_body(refs, amount=200_00),
                       headers=headers)
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_posted_entry_immutable_correction_via_reversal(client, refs, db):
    resp = client.post("/journals", json=balanced_body(refs),
                       headers={**ACCOUNTANT, "Idempotency-Key": "k-10"})
    entry_id = resp.json()["entry"]["id"]

    # 冲销凭证追加在开放期间
    rev = client.post(f"/journals/{entry_id}/reverse",
                      params={"period_id": refs["period_id"], "reason": "金额录入错误"},
                      headers=MANAGER)
    assert rev.status_code == 201
    rev_lines = rev.json()["lines"]
    assert rev_lines[0]["credit_cents"] == 100_00  # 借贷互换
    assert rev_lines[0]["debit_cents"] == 0

    # 原凭证未被修改
    original = db.get(JournalEntry, entry_id)
    assert original.lines[0].debit_cents == 100_00


def test_duplicate_reversal_rejected(client, refs):
    resp = client.post("/journals", json=balanced_body(refs),
                       headers={**ACCOUNTANT, "Idempotency-Key": "k-11"})
    entry_id = resp.json()["entry"]["id"]
    params = {"period_id": refs["period_id"], "reason": "第一次冲销"}
    assert client.post(f"/journals/{entry_id}/reverse", params=params,
                       headers=MANAGER).status_code == 201
    params["reason"] = "重复冲销尝试"
    resp = client.post(f"/journals/{entry_id}/reverse", params=params, headers=MANAGER)
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "ALREADY_REVERSED"


def test_auditor_is_read_only(client, refs):
    resp = client.post("/journals", json=balanced_body(refs),
                       headers={**AUDITOR, "Idempotency-Key": "k-12"})
    assert resp.status_code == 403
    # 但可以查询
    assert client.get("/journals", headers=AUDITOR).status_code == 200


def test_unauthenticated_rejected(client, refs):
    resp = client.post("/journals", json=balanced_body(refs),
                       headers={"Idempotency-Key": "k-13"})
    assert resp.status_code == 401
