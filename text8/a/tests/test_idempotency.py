"""幂等测试：重复请求返回原结果，指纹冲突 409 且不写历史。"""
from __future__ import annotations

from sqlalchemy import func, select

from app import engine
from app.enums import InstanceStatus, TaskStatus
from app.errors import ConflictError
from app.models import HistoryEvent, Instance, Task


def test_start_replay_returns_original(db, published_template, rid):
    req = rid()
    inst1, replay1 = engine.start_instance(db, "leave", "zhang", {"amount": 100}, req)
    assert replay1 is None
    inst2, replay2 = engine.start_instance(db, "leave", "zhang", {"amount": 100}, req)
    assert replay2 is not None
    assert replay2["instance_id"] == inst1.id
    # 只创建了一个实例
    count = db.scalar(select(func.count()).select_from(Instance))
    assert count == 1


def test_start_same_request_id_different_payload_conflict(db, published_template, rid):
    req = rid()
    engine.start_instance(db, "leave", "zhang", {"amount": 100}, req)
    try:
        engine.start_instance(db, "leave", "li", {"amount": 200}, req)
        assert False, "应当冲突"
    except ConflictError:
        pass
    # 仍然只有一个实例
    count = db.scalar(select(func.count()).select_from(Instance))
    assert count == 1


def test_decision_replay_returns_original_and_no_extra_history(db, published_template, rid):
    inst, _ = engine.start_instance(db, "leave", "zhang", {"amount": 100}, rid())
    task = db.scalar(
        select(Task).where(Task.instance_id == inst.id, Task.assignee == "alice")
    )
    req = rid()
    _, replay1 = engine.decide_task(
        db, inst.id, task.id, request_id=req, expected_version=1,
        actor="alice", decision="approve",
    )
    assert replay1 is None

    # 重复请求：返回原结果
    _, replay2 = engine.decide_task(
        db, inst.id, task.id, request_id=req, expected_version=1,
        actor="alice", decision="approve",
    )
    assert replay2 is not None
    assert replay2["instance_id"] == inst.id

    # task_approve 审计只有一条
    count = db.scalar(
        select(func.count())
        .select_from(HistoryEvent)
        .where(
            HistoryEvent.instance_id == inst.id,
            HistoryEvent.event_type == "task_approve",
        )
    )
    assert count == 1


def test_decision_request_id_reused_with_different_args_conflict(db, published_template, rid):
    inst, _ = engine.start_instance(db, "leave", "zhang", {"amount": 100}, rid())
    alice = db.scalar(
        select(Task).where(Task.instance_id == inst.id, Task.assignee == "alice")
    )
    bob = db.scalar(
        select(Task).where(Task.instance_id == inst.id, Task.assignee == "bob")
    )
    req = rid()
    engine.decide_task(db, inst.id, alice.id, request_id=req, expected_version=1,
                       actor="alice", decision="approve")
    # 同一 request_id 但参数不同（不同 task / actor）：冲突，bob 的待办不受影响
    try:
        engine.decide_task(db, inst.id, bob.id, request_id=req, expected_version=1,
                           actor="bob", decision="approve")
        assert False
    except ConflictError:
        pass
    db.refresh(bob)
    assert bob.status == TaskStatus.PENDING


def test_withdraw_idempotent(db, published_template, rid):
    inst, _ = engine.start_instance(db, "leave", "zhang", {"amount": 100}, rid())
    req = rid()
    engine.withdraw(db, inst.id, request_id=req, actor="zhang")
    _, replay = engine.withdraw(db, inst.id, request_id=req, actor="zhang")
    assert replay is not None
    db.refresh(inst)
    assert inst.status == InstanceStatus.WITHDRAWN
    count = db.scalar(
        select(func.count())
        .select_from(HistoryEvent)
        .where(
            HistoryEvent.instance_id == inst.id,
            HistoryEvent.event_type == "withdraw",
        )
    )
    assert count == 1


def test_rejected_flow_does_not_write_on_version_conflict(db, published_template, rid):
    inst, _ = engine.start_instance(db, "leave", "zhang", {"amount": 100}, rid())
    alice = db.scalar(
        select(Task).where(Task.instance_id == inst.id, Task.assignee == "alice")
    )
    before = db.scalar(
        select(func.count()).select_from(HistoryEvent).where(
            HistoryEvent.instance_id == inst.id
        )
    )
    try:
        engine.decide_task(db, inst.id, alice.id, request_id=rid(),
                           expected_version=2, actor="alice", decision="approve")
        assert False
    except ConflictError:
        pass
    after = db.scalar(
        select(func.count()).select_from(HistoryEvent).where(
            HistoryEvent.instance_id == inst.id
        )
    )
    assert before == after
    db.refresh(alice)
    assert alice.status == TaskStatus.PENDING
