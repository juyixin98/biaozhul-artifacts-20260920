"""期间关闭/重开，含关账与过账的竞争测试。"""
import threading

import pytest
from fastapi import HTTPException

from conftest import ACCOUNTANT, AUDITOR, MANAGER, TestingSessionLocal, balanced_body

from app.models import AuditLog, JournalEntry, Period
from app.services import periods as period_service
from app.services import posting


def test_close_blocks_posting(client, refs):
    assert client.post(f"/periods/{refs['period_id']}/close", headers=MANAGER).status_code == 200
    resp = client.post("/journals", json=balanced_body(refs),
                       headers={**ACCOUNTANT, "Idempotency-Key": "pc-1"})
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "PERIOD_CLOSED"


def test_double_close_rejected(client, refs):
    client.post(f"/periods/{refs['period_id']}/close", headers=MANAGER)
    resp = client.post(f"/periods/{refs['period_id']}/close", headers=MANAGER)
    assert resp.status_code == 409


def test_reopen_requires_manager_and_reason(client, refs, db):
    client.post(f"/periods/{refs['period_id']}/close", headers=MANAGER)

    # 会计不能重开
    resp = client.post(f"/periods/{refs['period_id']}/reopen",
                       json={"reason": "需要补录凭证"}, headers=ACCOUNTANT)
    assert resp.status_code == 403
    # 审计员不能重开
    resp = client.post(f"/periods/{refs['period_id']}/reopen",
                       json={"reason": "需要补录凭证"}, headers=AUDITOR)
    assert resp.status_code == 403
    # 原因太短被拒绝
    resp = client.post(f"/periods/{refs['period_id']}/reopen",
                       json={"reason": "补"}, headers=MANAGER)
    assert resp.status_code == 422
    # 财务负责人带原因重开成功，且留痕
    resp = client.post(f"/periods/{refs['period_id']}/reopen",
                       json={"reason": "年末审计调整需要补录凭证"}, headers=MANAGER)
    assert resp.status_code == 200
    log = db.query(AuditLog).filter_by(action="reopen_period").one()
    assert log.reason == "年末审计调整需要补录凭证"
    assert log.actor == "manager"

    # 重开后可以过账
    resp = client.post("/journals", json=balanced_body(refs),
                       headers={**ACCOUNTANT, "Idempotency-Key": "pc-2"})
    assert resp.status_code == 201


def test_close_and_post_race_has_consistent_outcome(db, refs):
    """关账与过账并发：结果必然等价于某个串行顺序。

    重复多轮：每轮要么过账成功后关账成功，要么关账成功后过账被拒绝；
    绝不允许凭证写入已关闭期间。
    """
    lines = [
        {"fund_id": refs["fund_id"], "department_id": refs["dept_id"],
         "account_id": refs["expense_id"], "debit_cents": 10_00, "credit_cents": 0},
        {"fund_id": refs["fund_id"], "department_id": refs["dept_id"],
         "account_id": refs["cash_id"], "debit_cents": 0, "credit_cents": 10_00},
    ]
    for round_no in range(10):
        period = Period(year=2030, month=round_no + 1, status="open")
        db.add(period)
        db.commit()
        outcomes = {}

        def do_post():
            session = TestingSessionLocal()
            try:
                posting.post_journal(
                    session, period_id=period.id, description="race", lines=lines,
                    actor="race", idempotency_key=f"race-{round_no}",
                )
                outcomes["post"] = "ok"
            except HTTPException as exc:
                outcomes["post"] = exc.detail["code"]
            finally:
                session.close()

        def do_close():
            session = TestingSessionLocal()
            try:
                period_service.close_period(session, period_id=period.id, actor="race")
                outcomes["close"] = "ok"
            except HTTPException as exc:
                outcomes["close"] = exc.detail["code"]
            finally:
                session.close()

        t1 = threading.Thread(target=do_post)
        t2 = threading.Thread(target=do_close)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        db.expire_all()
        final = db.get(Period, period.id)
        assert final.status == "closed"
        posted = db.query(JournalEntry).filter_by(period_id=period.id).count()
        # 串行可解释性：要么先过账（post ok, 1 张凭证），要么先关账（post 被拒，0 张）
        if outcomes["post"] == "ok":
            assert posted == 1
        else:
            assert outcomes["post"] == "PERIOD_CLOSED"
            assert posted == 0
