from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update

pytestmark = pytest.mark.db

from sqlalchemy.orm import sessionmaker

from workflow_engine.constants import TaskStatus
from workflow_engine.database import engine
from workflow_engine.models import NodeActivity, Task
from workflow_engine.services.instances import build_detail, get_instance
from workflow_engine.services.templates import get_instance_definition
from workflow_engine.timeouts import run_sweep


def _session():
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)()


def _expense_definition(timeout=1):
    return {
        "key": "timeout_tpl",
        "name": "超时流程",
        "nodes": [
            {"id": "start", "type": "start", "next": "mgr"},
            {
                "id": "mgr",
                "type": "approval",
                "name": "主管",
                "mode": "all",
                "approvers": ["alice"],
                "on_reject": "rejected_end",
                "timeout_seconds": timeout,
                "timeout_action": {"type": "escalate", "to": ["carol"]},
                "next": "cfo_gate",
            },
            {
                "id": "cfo_gate",
                "type": "condition",
                "branches": [{"when": "amount > 1000", "next": "cfo"}],
                "default": "approved_end",
            },
            {
                "id": "cfo",
                "type": "approval",
                "name": "CFO",
                "mode": "any",
                "approvers": ["cfo1", "cfo2"],
                "on_reject": "rejected_end",
                "timeout_seconds": timeout,
                "timeout_action": {"type": "auto_reject", "reason": "CFO 超时"},
                "next": "approved_end",
            },
            {"id": "approved_end", "type": "end", "outcome": "approved"},
            {"id": "rejected_end", "type": "end", "outcome": "rejected"},
        ],
    }


def _publish_and_start(client, context=None, definition=None, key="timeout_tpl"):
    definition = definition or _expense_definition()
    r = client.post("/templates", json={"definition": definition, "publish": True})
    assert r.status_code == 201, r.text
    r = client.post(
        "/instances",
        json={"template_key": key, "title": "超时单", "context": context or {"amount": 10}},
        headers={"X-User": "tom"},
    )
    assert r.status_code == 201, r.text
    return r.json()


def test_escalation_closes_old_and_creates_new_tasks(client):
    inst = _publish_and_start(client)
    iid = inst["id"]

    # 尚未到期：扫描无动作
    summary = run_sweep(_session(), get_definition=get_instance_definition)
    assert summary["processed"] == []

    detail = client.get(f"/instances/{iid}").json()
    assert {t["assignee"] for t in detail["pending_tasks"]} == {"alice"}

    # 把 deadline 调到过去，模拟超时
    _backdate(iid, "mgr", seconds=10)
    summary = run_sweep(_session(), get_definition=get_instance_definition)
    assert "mgr:escalated" in summary["processed"]
    assert summary["failed"] == []

    detail = client.get(f"/instances/{iid}").json()
    assert detail["status"] == "running"
    assert detail["current_node_id"] == "mgr"
    assert {t["assignee"] for t in detail["pending_tasks"]} == {"carol"}
    events = [h["event_type"] for h in detail["history"]]
    assert "escalate" in events

    # 升级人审批后继续推进 -> 小额 default -> approved
    r = client.post(
        f"/instances/{iid}/decide?node_id=mgr",
        json={"request_id": "esc-1", "expected_version": 1, "action": "approve"},
        headers={"X-User": "carol"},
    )
    assert r.status_code == 200 and r.json()["advanced"] is True
    assert client.get(f"/instances/{iid}").json()["status"] == "approved"


def test_escalation_happens_only_once(client):
    inst = _publish_and_start(client)
    iid = inst["id"]
    _backdate(iid, "mgr", seconds=10)

    s1 = run_sweep(_session(), get_definition=get_instance_definition)
    assert "mgr:escalated" in s1["processed"]

    # 升级只发生一次：升级后 deadline 已清空，再扫无动作
    s2 = run_sweep(_session(), get_definition=get_instance_definition)
    assert s2["processed"] == []

    # 即便强行再把 deadline 调到过去，escalated=True 也不会二次升级
    _backdate(iid, "mgr", seconds=10, allow_escalated=True)
    s3 = run_sweep(_session(), get_definition=get_instance_definition)
    assert "mgr:escalated" not in s3["processed"]
    detail = client.get(f"/instances/{iid}").json()
    assert {t["assignee"] for t in detail["pending_tasks"]} == {"carol"}


def test_auto_reject_timeout(client):
    definition = _expense_definition()
    inst = _publish_and_start(client, context={"amount": 5000}, definition=definition)
    iid = inst["id"]
    # alice 通过 -> 条件命中大额 -> cfo 节点
    r = client.post(
        f"/instances/{iid}/decide?node_id=mgr",
        json={"request_id": "a1", "expected_version": 1, "action": "approve"},
        headers={"X-User": "alice"},
    )
    assert r.json()["current_node_id"] == "cfo"

    _backdate(iid, "cfo", seconds=10)
    summary = run_sweep(_session(), get_definition=get_instance_definition)
    assert "cfo:auto_rejected" in summary["processed"]

    detail = client.get(f"/instances/{iid}").json()
    assert detail["status"] == "rejected"
    assert detail["current_node_id"] == "rejected_end"
    assert detail["reject_reason"] == "CFO 超时"
    assert detail["pending_tasks"] == []


def test_failed_sweep_is_retriable(client, monkeypatch):
    """处理中途失败：该实例整笔回滚，下轮扫描自动重试成功（任务可重试）。"""
    inst = _publish_and_start(client)
    iid = inst["id"]
    _backdate(iid, "mgr", seconds=10)

    calls = {"n": 0}
    from workflow_engine import timeouts as timeouts_mod

    original = timeouts_mod._process_instance

    def flaky(session, instance_id, now, definition):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom: simulated worker failure")
        return original(session, instance_id, now, definition)

    monkeypatch.setattr(timeouts_mod, "_process_instance", flaky)

    s1 = run_sweep(_session(), get_definition=get_instance_definition)
    assert s1["failed"] == [str(iid)]

    # 失败事务已回滚：旧待办仍在，没有 escalate 历史
    detail = client.get(f"/instances/{iid}").json()
    assert {t["assignee"] for t in detail["pending_tasks"]} == {"alice"}
    assert "escalate" not in [h["event_type"] for h in detail["history"]]

    # 恢复 monkeypatch 后下一轮成功
    monkeypatch.undo()
    s2 = run_sweep(_session(), get_definition=get_instance_definition)
    assert "mgr:escalated" in s2["processed"]
    detail = client.get(f"/instances/{iid}").json()
    assert {t["assignee"] for t in detail["pending_tasks"]} == {"carol"}


def test_restart_resumes_pending_escalations(client):
    """服务重启等价于重新执行 run_sweep：未完成的升级在重启后被继续处理。"""
    inst = _publish_and_start(client)
    iid = inst["id"]
    _backdate(iid, "mgr", seconds=3600)

    # 模拟"重启前扫描未运行"，直接在一个全新 session 里执行（等同重启后首轮）
    fresh = _session()
    summary = run_sweep(fresh, get_definition=get_instance_definition)
    assert "mgr:escalated" in summary["processed"]
    fresh.close()


def test_sweep_competes_safely_with_manual_approval(client):
    """升级扫描与人工审批并发：最终只有一种结果生效。"""
    import threading

    from tests.test_api import _engine_decide

    inst = _publish_and_start(client)
    iid = inst["id"]
    _backdate(iid, "mgr", seconds=10)

    barrier = threading.Barrier(2)
    results = {}

    def manual():
        barrier.wait()
        results["manual"] = _engine_decide(iid, None, "alice", "m-1")

    def sweep():
        barrier.wait()
        try:
            results["sweep"] = ("ok", run_sweep(_session(), get_definition=get_instance_definition))
        except Exception as exc:
            results["sweep"] = ("err", str(exc))

    t1 = threading.Thread(target=manual)
    t2 = threading.Thread(target=sweep)
    t1.start(); t2.start(); t1.join(); t2.join()

    detail = client.get(f"/instances/{iid}").json()
    # 只有一次状态转换：要么人工先批（approved），要么升级（仍在 mgr，待办给 carol）
    events = detail["history"]
    if detail["status"] == "approved":
        assert all("escalat" not in e["event_type"] for e in events)
        assert results["manual"][0] is True
    else:
        assert detail["current_node_id"] == "mgr"
        assert {t["assignee"] for t in detail["pending_tasks"]} == {"carol"}
        assert "mgr:escalated" in results["sweep"][1]["processed"]
    # 无论谁赢，alice 的待办都不能还挂着
    assert "alice" not in {t["assignee"] for t in detail["pending_tasks"]}


# ------------------------------------------------------------------ 工具


def _backdate(iid: int, node_id: str, *, seconds: int, allow_escalated: bool = False):
    session = _session()
    try:
        with session.begin():
            deadline = datetime.now(timezone.utc) - timedelta(seconds=seconds)
            stmt = (
                update(NodeActivity)
                .where(
                    NodeActivity.instance_id == iid,
                    NodeActivity.node_id == node_id,
                    NodeActivity.status == "pending",
                )
                .values(deadline=deadline)
            )
            if allow_escalated:
                stmt = stmt.where(NodeActivity.escalated.is_(True))
                # 升级分支会把 deadline 置空并阻止再次升级；这里模拟人工误改
            result = session.execute(stmt)
            assert result.rowcount == 1
    finally:
        session.close()
