"""预算预占/释放/转实际，含并发不超支测试。"""
import threading

import pytest
from fastapi import HTTPException

from conftest import ACCOUNTANT, MANAGER, TestingSessionLocal

from app.models import Budget, ExpenditureRequest
from app.services import budget as budget_service


def _make_request(db, budget_id, amount):
    return budget_service.create_request(
        db, budget_id=budget_id, amount_cents=amount, purpose="t", actor="test"
    )


def test_approve_encumbers_and_available_shrinks(client, budget):
    req = client.post("/requests", json={"budget_id": budget.id, "amount_cents": 400_00},
                      headers=ACCOUNTANT).json()
    assert client.post(f"/requests/{req['id']}/approve", headers=MANAGER).status_code == 200
    b = client.get(f"/budgets/{budget.id}", headers=ACCOUNTANT).json()
    assert b["encumbered_cents"] == 400_00
    assert b["available_cents"] == 600_00


def test_approve_beyond_available_rejected(client, budget):
    req = client.post("/requests", json={"budget_id": budget.id, "amount_cents": 1001_00},
                      headers=ACCOUNTANT).json()
    resp = client.post(f"/requests/{req['id']}/approve", headers=MANAGER)
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "INSUFFICIENT_BUDGET"


def test_post_releases_encumbrance_into_actual(client, budget):
    req = client.post("/requests", json={"budget_id": budget.id, "amount_cents": 300_00},
                      headers=ACCOUNTANT).json()
    client.post(f"/requests/{req['id']}/approve", headers=MANAGER)
    client.post(f"/requests/{req['id']}/post", headers=ACCOUNTANT)
    b = client.get(f"/budgets/{budget.id}", headers=ACCOUNTANT).json()
    assert b["encumbered_cents"] == 0
    assert b["actual_cents"] == 300_00
    assert b["available_cents"] == 700_00


def test_cancel_releases_encumbrance_exactly_once(client, budget):
    req = client.post("/requests", json={"budget_id": budget.id, "amount_cents": 300_00},
                      headers=ACCOUNTANT).json()
    client.post(f"/requests/{req['id']}/approve", headers=MANAGER)
    assert client.post(f"/requests/{req['id']}/cancel", headers=MANAGER).status_code == 200
    # 第二次取消被拒绝，预占不会重复释放
    resp = client.post(f"/requests/{req['id']}/cancel", headers=MANAGER)
    assert resp.status_code == 409
    b = client.get(f"/budgets/{budget.id}", headers=ACCOUNTANT).json()
    assert b["encumbered_cents"] == 0
    assert b["available_cents"] == 1000_00


def test_double_approve_rejected(client, budget):
    req = client.post("/requests", json={"budget_id": budget.id, "amount_cents": 100_00},
                      headers=ACCOUNTANT).json()
    assert client.post(f"/requests/{req['id']}/approve", headers=MANAGER).status_code == 200
    assert client.post(f"/requests/{req['id']}/approve", headers=MANAGER).status_code == 409


def test_concurrent_approvals_never_exceed_budget(db, budget):
    """预算 1000 元，两个 700 元申请并发批准：恰好一个成功。"""
    req1 = _make_request(db, budget.id, 700_00)
    req2 = _make_request(db, budget.id, 700_00)
    results = {}

    def approve(key, request_id):
        session = TestingSessionLocal()
        try:
            budget_service.approve_request(session, request_id=request_id, actor="race")
            results[key] = "ok"
        except HTTPException as exc:
            results[key] = exc.detail["code"]
        finally:
            session.close()

    threads = [threading.Thread(target=approve, args=("a", req1.id)),
               threading.Thread(target=approve, args=("b", req2.id))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results.values()) == ["INSUFFICIENT_BUDGET", "ok"]
    db.expire_all()
    b = db.get(Budget, budget.id)
    assert b.encumbered_cents == 700_00
    assert budget_service.available_cents(b) == 300_00


def test_concurrent_same_request_approved_once(db, budget):
    """同一申请并发批准两次：只预占一次。"""
    req = _make_request(db, budget.id, 500_00)
    outcomes = []

    def approve():
        session = TestingSessionLocal()
        try:
            budget_service.approve_request(session, request_id=req.id, actor="race")
            outcomes.append("ok")
        except HTTPException:
            outcomes.append("conflict")
        finally:
            session.close()

    threads = [threading.Thread(target=approve) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert outcomes.count("ok") == 1
    db.expire_all()
    assert db.get(Budget, budget.id).encumbered_cents == 500_00
    assert db.get(ExpenditureRequest, req.id).status == "approved"
