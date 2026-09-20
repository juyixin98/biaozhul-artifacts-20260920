"""全签/任签竞争、拒绝规则、撤回与权限测试。"""
from __future__ import annotations

import threading

import pytest
from sqlalchemy import select

from app import engine
from app.enums import InstanceStatus, TaskStatus
from app.errors import AuthzError, ConflictError
from app.models import HistoryEvent, Task


def _start(db, rid, context=None):
    inst, _ = engine.start_instance(
        db, "leave", "zhang", context or {"amount": 100}, rid()
    )
    return inst


def _tasks(db, inst_id, node_id="dept", status=TaskStatus.PENDING):
    return list(
        db.scalars(
            select(Task).where(
                Task.instance_id == inst_id,
                Task.node_id == node_id,
                Task.status == status,
            ).order_by(Task.id)
        )
    )


def test_all_mode_requires_every_approval(db, published_template, rid):
    inst = _start(db, rid)
    alice, bob = _tasks(db, inst.id)

    engine.decide_task(db, inst.id, alice.id, request_id=rid(),
                       expected_version=1, actor="alice",
                       decision="approve", comment=None)
    # 仅 alice 通过：仍停留在 dept，bob 的待办仍在
    db.expire_all()
    assert _tasks(db, inst.id)
    inst = db.get(type(inst), inst.id)
    assert inst.current_node_id == "dept"

    engine.decide_task(db, inst.id, bob.id, request_id=rid(),
                       expected_version=1, actor="bob",
                       decision="approve", comment=None)
    db.expire_all()
    inst = db.get(type(inst), inst.id)
    # amount=100 < 10000，走 default 直达 end
    assert inst.status == InstanceStatus.COMPLETED
    assert inst.current_node_id == "end"


def test_all_mode_single_rejection_rejects_flow(db, published_template, rid):
    inst = _start(db, rid)
    alice, bob = _tasks(db, inst.id)

    engine.decide_task(db, inst.id, bob.id, request_id=rid(),
                       expected_version=1, actor="bob",
                       decision="reject", comment="不同意")
    db.expire_all()
    inst = db.get(type(inst), inst.id)
    assert inst.status == InstanceStatus.REJECTED
    assert inst.reject_reason == "不同意"
    # 其余待办被关闭
    tasks = _tasks(db, inst.id)
    assert tasks == []
    statuses = {t.assignee: t.status for t in db.scalars(
        select(Task).where(Task.instance_id == inst.id))}
    assert statuses == {"alice": TaskStatus.CANCELED, "bob": TaskStatus.REJECTED}


def test_any_mode_first_approval_advances_and_cancels_siblings(
    db, published_template, rid
):
    # amount >= 10000 才进入 gm(any) 节点
    inst = _start(db, rid, {"amount": 50000})
    alice, bob = _tasks(db, inst.id)
    engine.decide_task(db, inst.id, alice.id, request_id=rid(),
                       expected_version=1, actor="alice", decision="approve")
    engine.decide_task(db, inst.id, bob.id, request_id=rid(),
                       expected_version=1, actor="bob", decision="approve")

    db.expire_all()
    gm_tasks = _tasks(db, inst.id, node_id="gm")
    assert {t.assignee for t in gm_tasks} == {"carol", "dave"}
    carol = gm_tasks[0]

    engine.decide_task(db, inst.id, carol.id, request_id=rid(),
                       expected_version=1, actor="carol", decision="approve")
    db.expire_all()
    inst = db.get(type(inst), inst.id)
    assert inst.status == InstanceStatus.COMPLETED
    # dave 的待办被关闭
    statuses = {t.assignee: t.status for t in db.scalars(
        select(Task).where(Task.instance_id == inst.id, Task.node_id == "gm"))}
    assert statuses["carol"] == TaskStatus.APPROVED
    assert statuses["dave"] == TaskStatus.CANCELED


def test_any_mode_rejection_then_approval_still_advances(db, published_template, rid):
    inst = _start(db, rid, {"amount": 50000})
    for t in _tasks(db, inst.id):
        engine.decide_task(db, inst.id, t.id, request_id=rid(),
                           expected_version=1, actor=t.assignee, decision="approve")

    db.expire_all()
    carol, dave = _tasks(db, inst.id, node_id="gm")
    # carol 先拒绝，但 dave 还能处理
    engine.decide_task(db, inst.id, carol.id, request_id=rid(),
                       expected_version=1, actor="carol", decision="reject")
    db.expire_all()
    inst = db.get(type(inst), inst.id)
    assert inst.status == InstanceStatus.RUNNING
    assert _tasks(db, inst.id, node_id="gm")

    engine.decide_task(db, inst.id, dave.id, request_id=rid(),
                       expected_version=1, actor="dave", decision="approve")
    db.expire_all()
    inst = db.get(type(inst), inst.id)
    assert inst.status == InstanceStatus.COMPLETED


def test_any_mode_all_rejected_rejects_flow(db, published_template, rid):
    inst = _start(db, rid, {"amount": 50000})
    for t in _tasks(db, inst.id):
        engine.decide_task(db, inst.id, t.id, request_id=rid(),
                           expected_version=1, actor=t.assignee, decision="approve")

    db.expire_all()
    carol, dave = _tasks(db, inst.id, node_id="gm")
    engine.decide_task(db, inst.id, carol.id, request_id=rid(),
                       expected_version=1, actor="carol", decision="reject",
                       comment="c no")
    engine.decide_task(db, inst.id, dave.id, request_id=rid(),
                       expected_version=1, actor="dave", decision="reject",
                       comment="d no")
    db.expire_all()
    inst = db.get(type(inst), inst.id)
    assert inst.status == InstanceStatus.REJECTED


def test_concurrent_approvals_only_one_transition(db, published_template, rid):
    """并发抢同一个待办：只有一次有效状态转换。"""
    inst = _start(db, rid)
    alice_task = _tasks(db, inst.id)[0]
    task_id = alice_task.id

    results: list[Exception | str] = []
    barrier = threading.Barrier(2)

    def actor_decide(name: str):
        from app.db import SessionLocal
        with SessionLocal() as s:
            try:
                barrier.wait(timeout=10)
                engine.decide_task(
                    s, inst.id, task_id, request_id=rid(),
                    expected_version=1, actor=name, decision="approve",
                )
                results.append("ok")
            except Exception as exc:  # noqa: BLE001
                results.append(exc)

    t1 = threading.Thread(target=actor_decide, args=("alice",))
    t2 = threading.Thread(target=actor_decide, args=("alice",))
    t1.start(); t2.start()
    t1.join(15); t2.join(15)

    oks = [r for r in results if r == "ok"]
    errs = [r for r in results if r != "ok"]
    assert len(oks) == 1, results
    assert isinstance(errs[0], ConflictError)

    db.expire_all()
    task = db.get(Task, task_id)
    assert task.status == TaskStatus.APPROVED
    # 只产生一条 task_approve 审计
    from sqlalchemy import func

    count = db.scalar(
        select(func.count())
        .select_from(HistoryEvent)
        .where(
            HistoryEvent.instance_id == inst.id,
            HistoryEvent.event_type == "task_approve",
        )
    )
    assert count == 1


def test_only_assignee_may_act(db, published_template, rid):
    inst = _start(db, rid)
    alice = _tasks(db, inst.id)[0]
    with pytest.raises(AuthzError):
        engine.decide_task(db, inst.id, alice.id, request_id=rid(),
                           expected_version=1, actor="mallory",
                           decision="approve")
    # 替换实例 ID 也不能越权：用另一个实例的 task_id 操作本实例
    other = _start(db, rid)
    other_task = _tasks(db, other.id)[0]
    with pytest.raises(Exception):
        engine.decide_task(db, inst.id, other_task.id, request_id=rid(),
                           expected_version=1, actor="alice", decision="approve")


def test_submitter_can_withdraw_before_end(db, published_template, rid):
    inst = _start(db, rid)
    engine.withdraw(db, inst.id, request_id=rid(), actor="zhang")
    db.expire_all()
    inst = db.get(type(inst), inst.id)
    assert inst.status == InstanceStatus.WITHDRAWN
    assert _tasks(db, inst.id) == []


def test_non_submitter_cannot_withdraw(db, published_template, rid):
    inst = _start(db, rid)
    with pytest.raises(AuthzError):
        engine.withdraw(db, inst.id, request_id=rid(), actor="mallory")
    db.expire_all()
    inst = db.get(type(inst), inst.id)
    assert inst.status == InstanceStatus.RUNNING


def test_cannot_withdraw_after_completion(db, published_template, rid):
    inst = _start(db, rid, {"amount": 100})
    for t in _tasks(db, inst.id):
        engine.decide_task(db, inst.id, t.id, request_id=rid(),
                           expected_version=1, actor=t.assignee,
                           decision="approve")
    db.expire_all()
    inst = db.get(type(inst), inst.id)
    assert inst.status == InstanceStatus.COMPLETED
    with pytest.raises(ConflictError):
        engine.withdraw(db, inst.id, request_id=rid(), actor="zhang")
