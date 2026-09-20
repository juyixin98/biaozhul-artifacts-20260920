from __future__ import annotations

from tests.conftest import ACCOUNTANT_KEY, AUDITOR_KEY, auth

CSV_HEADER = (
    "voucher_no,entry_date,period_code,description,line_no,"
    "fund_code,department_code,account_code,debit_cents,credit_cents,"
    "reservation_request_no,line_description\n"
)


def _csv(*vouchers):
    rows = [CSV_HEADER]
    for vno, amount in vouchers:
        rows.append(
            f"{vno},2026-09-10,2026-09,batch,1,GF,ADMIN,5100,{amount},0,,expense\n"
        )
        rows.append(
            f"{vno},2026-09-10,2026-09,batch,2,GF,ADMIN,1010,0,{amount},,cash\n"
        )
    return "".join(rows).encode("utf-8")


def test_csv_happy_path_posts_all(client, base_world):
    r = client.post(
        "/entries/csv",
        files={"file": ("entries.csv", _csv(("JV-C1", 100_00), ("JV-C2", 200_00)), "text/csv")},
        headers=auth(ACCOUNTANT_KEY),
    )
    assert r.status_code == 201, r.text
    assert len(r.json()["posted_entries"]) == 2
    usage = client.get(
        "/reports/budget-usage?year=2026&fund_code=GF&department_code=ADMIN&account_code=5100",
        headers=auth(AUDITOR_KEY),
    ).json()
    assert usage[0]["actual_cents"] == 300_00


def test_csv_any_error_rolls_back_whole_batch(client, base_world):
    # One unbalanced voucher fails balancing during posting.
    bad = (
        CSV_HEADER
        + "JV-OK,2026-09-10,2026-09,batch,1,GF,ADMIN,5100,10000,0,,e\n"
        + "JV-OK,2026-09-10,2026-09,batch,2,GF,ADMIN,1010,0,9999,,c\n"
    ).encode("utf-8")
    r = client.post(
        "/entries/csv",
        files={"file": ("bad.csv", bad, "text/csv")},
        headers=auth(ACCOUNTANT_KEY),
    )
    assert r.status_code == 422
    assert "CSV error" in r.json()["error"]["message"] or "not balanced" in str(
        r.json()["error"]
    )
    listing = client.get("/entries?period_code=2026-09", headers=auth(AUDITOR_KEY)).json()
    assert listing == []


def test_csv_over_row_limit_rejected(client, base_world):
    rows = [CSV_HEADER]
    # Each voucher has 2 lines, so 1001 vouchers => 2002 data rows.
    for i in range(1001):
        vno = f"JV-L{i:04d}"
        rows.append(f"{vno},2026-09-10,2026-09,batch,1,GF,ADMIN,5100,1,0,,e\n")
        rows.append(f"{vno},2026-09-10,2026-09,batch,2,GF,ADMIN,1010,0,1,,c\n")
    r = client.post(
        "/entries/csv",
        files={"file": ("big.csv", "".join(rows).encode(), "text/csv")},
        headers=auth(ACCOUNTANT_KEY),
    )
    assert r.status_code == 422


def test_csv_bad_encoding_rejected(client, base_world):
    r = client.post(
        "/entries/csv",
        files={"file": ("x.csv", b"\xff\xfe not csv", "text/csv")},
        headers=auth(ACCOUNTANT_KEY),
    )
    assert r.status_code == 422


def test_csv_invalid_line_data_rolls_back_whole_batch(client, base_world):
    # Unknown account reference: rejected during posting; the whole batch
    # (including the otherwise valid first voucher) must be absent afterwards.
    content = (
        CSV_HEADER
        + "JV-Z1,2026-09-10,2026-09,batch,1,GF,ADMIN,9999,100,0,,e\n"
        + "JV-Z1,2026-09-10,2026-09,batch,2,GF,ADMIN,1010,0,100,,c\n"
    ).encode("utf-8")
    r = client.post(
        "/entries/csv",
        files={"file": ("bad.csv", content, "text/csv")},
        headers=auth(ACCOUNTANT_KEY),
    )
    assert r.status_code == 422
    assert "9999" in r.json()["error"]["message"]
    listing = client.get("/entries?period_code=2026-09", headers=auth(AUDITOR_KEY)).json()
    assert listing == []


def test_csv_non_integer_amount_rolls_back_whole_batch(client, base_world):
    content = (
        CSV_HEADER
        + "JV-Z2,2026-09-10,2026-09,batch,1,GF,ADMIN,5100,abc,0,,e\n"
        + "JV-Z2,2026-09-10,2026-09,batch,2,GF,ADMIN,1010,0,100,,c\n"
    ).encode("utf-8")
    r = client.post(
        "/entries/csv",
        files={"file": ("bad.csv", content, "text/csv")},
        headers=auth(ACCOUNTANT_KEY),
    )
    assert r.status_code == 422
    assert "not an integer" in str(r.json()["error"]["details"])


def test_csv_replay_with_idempotency_key(client, base_world):
    body = _csv(("JV-RC", 100_00))
    headers = {**auth(ACCOUNTANT_KEY), "Idempotency-Key": "csv-key-1"}
    r1 = client.post(
        "/entries/csv",
        files={"file": ("entries.csv", body, "text/csv")},
        headers=headers,
    )
    r2 = client.post(
        "/entries/csv",
        files={"file": ("entries.csv", body, "text/csv")},
        headers=headers,
    )
    assert r1.status_code == r2.status_code == 201
    listing = client.get("/entries?voucher_no=JV-RC", headers=auth(AUDITOR_KEY)).json()
    assert len(listing) == 1
