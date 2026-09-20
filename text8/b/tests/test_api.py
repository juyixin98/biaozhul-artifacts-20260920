import threading

import pytest

pytestmark = pytest.mark.db


def _decide(client, iid, *, user, request_id, version, action="approve", node="approve1", comment=None):
    body = {"request_id": request_id, "expected_version": version, "action": action}
    if comment is not None:
        body["comment"] = comment
    return client.post(
        f"/instances/{iid}/decide?node_id={node}",
        json=body,
        headers={"X-User": user},
    )


# ----------------------------------------------------------- 全签 / 任签


def test_all_mode_requires_everyone(start_instance, client):
    inst = start_instance(context={"amount": 0})
    iid, ver = inst["id"], inst["version_number"]

    r = _decide(client, iid, user="alice", request_id="a1", version=ver)
    assert r.status_code == 200 and r.json()["advanced"] is False
    detail = client.get(f"/instances/{iid}").json()
    assert detail["current_node_id"] == "approve1"
    assert {t["assignee"] for t in detail["pending_tasks"]} == {"bob"}

    r = _decide(client, iid, user="bob", request_id="b1", version=ver)
    assert r.status_code == 200 and r.json()["advanced"] is True
    detail = client.get(f"/instances/{iid}").json()
    assert detail["status"] == "approved"
    assert detail["current_node_id"] == "approved_end"


def test_all_mode_explicit_reject_closes_others(start_instance, client):
    inst = start_instance(context={"amount": 0})
    iid, ver = inst["id"], inst["version_number"]

    r = _decide(client, iid, user="alice", request_id="a1", version=ver, action="approve")
    assert r.status_code == 200
    r = _decide(client, iid, user="bob", request_id="b1", version=ver, action="reject", comment="不行")
    assert r.status_code == 200 and r.json()["advanced"] is True

    detail = client.get(f"/instances/{iid}").json()
    assert detail["status"] == "rejected"
    assert detail["reject_reason"] == "不行"
    assert detail["current_node_id"] == "rejected_end"
    # 没有遗留待办
    assert detail["pending_tasks"] == []


def test_any_mode_first_approval_closes_rest(start_instance, client):
    # 大额 -> approve2(any: carol/dave)
    inst = start_instance(context={"amount": 500})
    iid, ver = inst["id"], inst["version_number"]
    assert _decide(client, iid, user="alice", request_id="a1", version=ver).json()["advanced"] is False
    assert _decide(client, iid, user="bob", request_id="b1", version=ver).json()["advanced"] is True
    detail = client.get(f"/instances/{iid}").json()
    assert detail["current_node_id"] == "approve2"
    assert {t["assignee"] for t in detail["pending_tasks"]} == {"carol", "dave"}

    r = _decide(client, iid, user="carol", request_id="c1", version=ver, node="approve2")
    assert r.json()["advanced"] is True
    detail = client.get(f"/instances/{iid}").json()
    assert detail["status"] == "approved"
    # dave 的待办被关闭：历史里有 close_tasks
    close_events = [h for h in detail["history"] if h["event_type"] == "close_tasks"]
    assert any("dave" in h["detail"]["closed"] for h in close_events)


def test_any_mode_reject(start_instance, client):
    inst = start_instance(context={"amount": 500})
    iid, ver = inst["id"], inst["version_number"]
    _decide(client, iid, user="alice", request_id="a1", version=ver)
    _decide(client, iid, user="bob", request_id="b1", version=ver)
    r = _decide(client, iid, user="dave", request_id="d1", version=ver, node="approve2", action="reject")
    detail = client.get(f"/instances/{iid}").json()
    assert detail["status"] == "rejected"


# ----------------------------------------------------------- 会签竞争（真并发）
#
# TestClient 自身串行化请求，无法压出数据库竞争。下面两个测试各自开独立
# DB 连接，在 Barrier 上同时提交，直接验证行锁语义。


def _engine_decide(iid, definition, user, rid, version=1, node="approve1", action="approve"):
    """在独立连接/事务里完成一次审批，返回 (ok, payload_or_error)。"""
    from sqlalchemy.orm import sessionmaker

    from workflow_engine.api.dependencies import run_idempotent
    from workflow_engine.database import engine
    from workflow_engine.engine import FlowEngine
    from workflow_engine.services import instances as instance_service
    from workflow_engine.services.templates import get_instance_definition

    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)
    session = Session()
    try:
        def execute():
            instance = instance_service.get_instance(session, iid)
            instance_service.check_expected_version(instance, version)
            return FlowEngine(session).decide(
                instance_id=iid,
                definition=get_instance_definition(session, instance),
                actor=user,
                action=action,
                task_id=None,
                node_id=node,
                comment=None,
                request_id=rid,
            )

        payload, replayed = run_idempotent(session, rid, execute)
        return True, payload
    except Exception as exc:
        return False, f"{type(exc).__name__}: {getattr(exc, 'code', exc)}"
    finally:
        session.close()


def test_concurrent_all_sign_only_one_advances(start_instance, client):
    """全签最后两票（其中一票来自非审批人）同时提交：合法票恰好推进一次。"""
    inst = start_instance(context={"amount": 0})
    iid = inst["id"]
    # alice 先过，剩 bob 一票；eve 不是审批人
    assert _decide(client, iid, user="alice", request_id="a1", version=1).status_code == 200

    barrier = threading.Barrier(2)
    results = []

    def vote(user, rid):
        barrier.wait()
        results.append((user, *_engine_decide(iid, None, user, rid)))

    t1 = threading.Thread(target=vote, args=("bob", "b1"))
    t2 = threading.Thread(target=vote, args=("eve", "b2"))
    t1.start(); t2.start(); t1.join(); t2.join()

    detail = client.get(f"/instances/{iid}").json()
    assert detail["status"] == "approved"
    assert sum(h["event_type"] == "complete" for h in detail["history"]) == 1
    by_user = {u: (ok, p) for u, ok, p in results}
    assert by_user["bob"][0] is True and by_user["bob"][1]["advanced"] is True
    assert by_user["eve"][0] is False


def test_concurrent_any_sign_single_advance(start_instance):
    """任签节点两名审批人同时通过：只有一人推进，另一人得到冲突。"""
    inst = start_instance(context={"amount": 500})
    iid = inst["id"]
    # 走完全签首节点
    assert _engine_decide(iid, None, "alice", "a1")[0]
    assert _engine_decide(iid, None, "bob", "b1")[1]["advanced"] is True

    barrier = threading.Barrier(2)
    results = []

    def vote(user, rid):
        barrier.wait()
        results.append((user, *_engine_decide(iid, None, user, rid, node="approve2")))

    t1 = threading.Thread(target=vote, args=("carol", "c1"))
    t2 = threading.Thread(target=vote, args=("dave", "d1"))
    t1.start(); t2.start(); t1.join(); t2.join()

    successes = [(u, p) for u, ok, p in results if ok]
    failures = [(u, p) for u, ok, p in results if not ok]
    assert len(successes) == 1, results
    assert successes[0][1]["advanced"] is True
    assert len(failures) == 1

    from sqlalchemy.orm import sessionmaker

    from workflow_engine.database import engine
    from workflow_engine.services.instances import build_detail, get_instance

    session = sessionmaker(bind=engine, future=True)()
    try:
        detail = build_detail(session, get_instance(session, iid))
    finally:
        session.close()
    assert detail["status"] == "approved"
    assert sum(h["event_type"] == "complete" for h in detail["history"]) == 1


def test_concurrent_double_reject_single_transition(start_instance, client):
    """全签节点两名审批人同时拒绝：只允许一次拒绝转换。"""
    inst = start_instance(context={"amount": 0})
    iid = inst["id"]

    barrier = threading.Barrier(2)
    results = []

    def vote(user, rid):
        barrier.wait()
        results.append((user, *_engine_decide(iid, None, user, rid, action="reject")))

    t1 = threading.Thread(target=vote, args=("alice", "r1"))
    t2 = threading.Thread(target=vote, args=("bob", "r2"))
    t1.start(); t2.start(); t1.join(); t2.join()

    successes = [r for r in results if r[1]]
    assert len(successes) == 1, results
    detail = client.get(f"/instances/{iid}").json()
    assert detail["status"] == "rejected"
    assert sum(h["event_type"] == "complete" for h in detail["history"]) == 1
    assert detail["current_node_id"] == "rejected_end"


# ----------------------------------------------------------- 幂等


def test_idempotent_replay_returns_original(start_instance, client):
    inst = start_instance(context={"amount": 0})
    iid, ver = inst["id"], inst["version_number"]
    r1 = _decide(client, iid, user="alice", request_id="dup-1", version=ver)
    r2 = _decide(client, iid, user="alice", request_id="dup-1", version=ver)
    r3 = _decide(client, iid, user="alice", request_id="dup-1", version=ver)
    assert r1.json()["idempotent_replay"] is False
    assert r2.json()["idempotent_replay"] is True
    assert r3.json()["idempotent_replay"] is True
    detail = client.get(f"/instances/{iid}").json()
    assert sum(h["event_type"] == "approve" for h in detail["history"]) == 1


def test_conflict_does_not_consume_request_id(start_instance, client):
    inst = start_instance(context={"amount": 0})
    iid, ver = inst["id"], inst["version_number"]
    # 错误版本 -> 冲突，不写台账
    r = _decide(client, iid, user="alice", request_id="reuse-1", version=99)
    assert r.status_code == 409
    # 同 request_id 修正后可用
    r = _decide(client, iid, user="alice", request_id="reuse-1", version=ver)
    assert r.status_code == 200 and r.json()["idempotent_replay"] is False


def test_replay_works_even_with_different_actor(start_instance, client):
    """重复请求即使换 X-User，也只回放首次结果，不产生新转换。"""
    inst = start_instance(context={"amount": 0})
    iid, ver = inst["id"], inst["version_number"]
    _decide(client, iid, user="alice", request_id="rep-2", version=ver)
    r = _decide(client, iid, user="bob", request_id="rep-2", version=ver)
    assert r.json()["idempotent_replay"] is True


# ----------------------------------------------------------- 权限


def test_only_assignee_can_act(start_instance, client):
    inst = start_instance(context={"amount": 0})
    iid, ver = inst["id"], inst["version_number"]
    r = _decide(client, iid, user="mallory", request_id="x1", version=ver)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "task_not_pending"


def test_task_id_must_belong_to_instance(start_instance, client):
    inst1 = start_instance(context={"amount": 0}, key="test_tpl")
    inst2 = start_instance(context={"amount": 0}, key="test_tpl", title="第二单")
    detail1 = client.get(f"/instances/{inst1['id']}").json()
    task_id = detail1["pending_tasks"][0]["id"]

    # 拿实例2的路径 + 实例1的 task_id —— 不能靠替换实例 ID 越权
    body = {"request_id": "cross-1", "expected_version": inst2["version_number"], "action": "approve"}
    r = client.post(
        f"/instances/{inst2['id']}/decide?task_id={task_id}",
        json=body,
        headers={"X-User": "alice"},
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] in {"task_not_pending", "task_instance_mismatch"}


def test_only_submitter_can_withdraw(start_instance, client):
    inst = start_instance(context={"amount": 0})
    iid, ver = inst["id"], inst["version_number"]
    r = client.post(
        f"/instances/{iid}/withdraw",
        json={"request_id": "w1", "expected_version": ver},
        headers={"X-User": "mallory"},
    )
    assert r.status_code == 409 and r.json()["error"]["code"] == "not_submitter"

    r = client.post(
        f"/instances/{iid}/withdraw",
        json={"request_id": "w2", "expected_version": ver},
        headers={"X-User": "tom"},
    )
    assert r.status_code == 200
    detail = client.get(f"/instances/{iid}").json()
    assert detail["status"] == "withdrawn"
    assert detail["pending_tasks"] == []
    # 结束后不能再撤回
    r = client.post(
        f"/instances/{iid}/withdraw",
        json={"request_id": "w3", "expected_version": ver},
        headers={"X-User": "tom"},
    )
    assert r.status_code == 409


def test_decide_on_finished_instance_conflicts(client, start_instance):
    inst = start_instance(context={"amount": 0})
    iid, ver = inst["id"], inst["version_number"]
    _decide(client, iid, user="alice", request_id="a1", version=ver)
    _decide(client, iid, user="bob", request_id="b1", version=ver)
    r = _decide(client, iid, user="alice", request_id="a2", version=ver)
    assert r.status_code == 409


# ----------------------------------------------------------- 条件分支


def test_condition_branch_routing(client, start_instance):
    start_instance(context={"amount": 0})  # 确保模板已发布
    for amount, expect_node in [(10, "approved_end"), (500, "approve2")]:
        r = client.post(
            "/instances",
            json={"template_key": "test_tpl", "title": f"x{amount}", "context": {"amount": amount}},
            headers={"X-User": "tom"},
        )
        iid, ver = r.json()["id"], r.json()["version_number"]
        _decide(client, iid, user="alice", request_id=f"a-{amount}", version=ver)
        final = _decide(client, iid, user="bob", request_id=f"b-{amount}", version=ver)
        assert final.json()["current_node_id"] == expect_node
