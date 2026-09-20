"""CSV 导入：归组、行数上限、整批回滚。"""

from conftest import ACCOUNTANT

from app.models import JournalEntry

CSV_OK = """voucher_no,period,fund_code,department_code,account_code,debit_cents,credit_cents,description
V-1,2026-01,GF,PW,5001,10000,0,办公费
V-1,2026-01,GF,PW,1001,0,10000,办公费
V-2,2026-01,GF,PW,5001,20000,0,差旅费
V-2,2026-01,GF,PW,1001,0,20000,差旅费
"""

# 第二张凭证借贷不平 -> 整批回滚
CSV_BAD = """voucher_no,period,fund_code,department_code,account_code,debit_cents,credit_cents,description
V-1,2026-01,GF,PW,5001,10000,0,办公费
V-1,2026-01,GF,PW,1001,0,10000,办公费
V-2,2026-01,GF,PW,5001,20000,0,坏凭证
V-2,2026-01,GF,PW,1001,0,9999,坏凭证
"""


def _upload(client, content: str):
    return client.post(
        "/journals/import",
        files={"file": ("journals.csv", content.encode(), "text/csv")},
        headers=ACCOUNTANT,
    )


def test_import_groups_by_voucher(client, refs, db):
    resp = _upload(client, CSV_OK)
    assert resp.status_code == 201
    assert resp.json()["vouchers"] == 2
    entries = db.query(JournalEntry).all()
    assert len(entries) == 2
    assert all(len(e.lines) == 2 for e in entries)


def test_import_batch_rolls_back_on_any_error(client, refs, db):
    resp = _upload(client, CSV_BAD)
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "UNBALANCED"
    # 整批回滚：第一张合法凭证也不能留下
    assert db.query(JournalEntry).count() == 0


def test_import_rejects_over_2000_rows(client, refs):
    header = "voucher_no,period,fund_code,department_code,account_code,debit_cents,credit_cents,description\n"
    row = "V-X,2026-01,GF,PW,5001,1,0,x\n"
    resp = _upload(client, header + row * 2001)
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "TOO_MANY_ROWS"


def test_import_rejects_unknown_code_and_rolls_back(client, refs, db):
    csv_text = CSV_OK + "V-3,2026-01,GF,NOPE,5001,5,0,坏部门\nV-3,2026-01,GF,PW,1001,0,5,坏部门\n"
    resp = _upload(client, csv_text)
    assert resp.status_code == 422
    assert db.query(JournalEntry).count() == 0
