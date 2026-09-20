import time

import pytest
from datetime import datetime, timedelta, timezone
from sqlalchemy import update
from sqlalchemy.orm import sessionmaker

pytestmark = pytest.mark.db

from workflow_engine.config import Settings
from workflow_engine.database import engine
from workflow_engine.models import NodeActivity
from workflow_engine.sweeper import TimeoutSweeper


def test_sweeper_thread_processes_due(client):
    """启动后台线程，验证它自动处理到期升级（替代外部调度/消息队列）。"""
    # 用演示模板（超时 2 秒）
    r = client.post("/demo/seed")
    assert r.status_code == 200
    r = client.post(
        "/instances",
        json={"template_key": "expense", "title": "线程扫描测试", "context": {"amount": 1}},
        headers={"X-User": "tom"},
    )
    iid = r.json()["id"]
    assert r.json()["current_node_id"] == "manager"

    # 直接把 deadline 改为过去（不必真的等 2 秒）
    Session = sessionmaker(bind=engine, future=True)
    s = Session()
    with s.begin():
        s.execute(
            update(NodeActivity)
            .where(NodeActivity.instance_id == iid, NodeActivity.node_id == "manager")
            .values(deadline=datetime.now(timezone.utc) - timedelta(seconds=5))
        )
    s.close()

    settings = Settings(
        database_url=engine.url.render_as_string(hide_password=False),
        enable_sweeper=True,
        sweep_interval_seconds=0.2,
    )
    sweeper = TimeoutSweeper(settings)
    sweeper.start()
    try:
        for _ in range(50):
            detail = client.get(f"/instances/{iid}").json()
            assignees = {t["assignee"] for t in detail["pending_tasks"]}
            if assignees == {"carol"}:
                break
            time.sleep(0.1)
        else:
            pytest.fail("后台扫描未在预期时间内完成升级")
    finally:
        sweeper.stop()

    detail = client.get(f"/instances/{iid}").json()
    assert detail["status"] == "running"
    assert [h["event_type"] for h in detail["history"]].count("escalate") == 1


def test_demo_full_flow_via_api(client):
    """demo 种子幂等 + 大额流程：全签 -> 条件 -> 任签 -> 通过。"""
    assert client.post("/demo/seed").json()["created"] is True
    assert client.post("/demo/seed").json()["created"] is False

    r = client.post(
        "/instances",
        json={"template_key": "expense", "title": "大额报销", "context": {"amount": 50000, "overseas": False}},
        headers={"X-User": "tom"},
    )
    assert r.status_code == 201
    iid, ver = r.json()["id"], r.json()["version_number"]

    for user, rid in [("alice", "d1"), ("bob", "d2")]:
        r = client.post(
            f"/instances/{iid}/decide?node_id=manager",
            json={"request_id": rid, "expected_version": ver, "action": "approve"},
            headers={"X-User": user},
        )
        assert r.status_code == 200, r.text

    detail = client.get(f"/instances/{iid}").json()
    assert detail["current_node_id"] == "cfo"

    r = client.post(
        f"/instances/{iid}/decide?node_id=cfo",
        json={"request_id": "d3", "expected_version": ver, "action": "approve"},
        headers={"X-User": "cfo1"},
    )
    assert r.json()["advanced"] is True
    detail = client.get(f"/instances/{iid}").json()
    assert detail["status"] == "approved"
    assert detail["current_node_id"] == "approved_end"
    # cfo2 的待办已被任签关闭
    assert detail["pending_tasks"] == []


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}
