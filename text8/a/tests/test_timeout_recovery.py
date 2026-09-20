"""超时升级与重启恢复测试。"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select

from app import engine
from app.db import SessionLocal
from app.enums import EscalationStatus, InstanceStatus, TaskStatus
from app.models import Escalation, HistoryEvent, Task


def _force_due(db, instance_id, node_id="dept"):
    esc = db.scalar(
        select(Escalation).where(
            Escalation.instance_id == instance_id,
            Escalation.node_id == node_id,
        )
    )
    assert esc is not None
    from app.models import utcnow

    esc.due_at = utcnow() - timedelta(seconds=1)
    db.commit()
    return esc


def test_escalation_reassigns_pending_tasks(db, published_template, rid):
    inst, _ = engine.start_instance(db, "leave", "zhang", {"amount": 100}, rid())
    _force_due(db, inst.id)

    processed = engine.process_due_escalations(db, limit=10)
    assert processed == 1

    db.expire_all()
    # 旧待办 canceled，升级目标 frank 拿到新 pending 待办
    statuses = {
        (t.assignee, t.status)
        for t in db.scalars(select(Task).where(Task.instance_id == inst.id))
    }
    assert ("alice", TaskStatus.CANCELED) in statuses
    assert ("bob", TaskStatus.CANCELED) in statuses
    assert ("frank", TaskStatus.PENDING) in statuses

    esc = db.scalar(
        select(Escalation).where(Escalation.instance_id == inst.id)
    )
    assert esc.status == EscalationStatus.DONE

    # frank 审批后流程继续
    frank_task = db.scalar(
        select(Task).where(
            Task.instance_id == inst.id,
            Task.assignee == "frank",
            Task.status == TaskStatus.PENDING,
        )
    )
    engine.decide_task(db, inst.id, frank_task.id, request_id=rid(),
                       expected_version=1, actor="frank", decision="approve")
    db.expire_all()
    inst = db.get(type(inst), inst.id)
    assert inst.status == InstanceStatus.COMPLETED


def test_escalation_applies_only_once(db, published_template, rid):
    inst, _ = engine.start_instance(db, "leave", "zhang", {"amount": 100}, rid())
    _force_due(db, inst.id)
    assert engine.process_due_escalations(db, limit=10) == 1
    # 再跑一次：没有新的到期升级
    assert engine.process_due_escalations(db, limit=10) == 0
    db.expire_all()
    n_frank = db.scalar(
        select(func.count())
        .select_from(Task)
        .where(Task.instance_id == inst.id, Task.assignee == "frank")
    )
    assert n_frank == 1


def test_escalation_canceled_after_approval(db, published_template, rid):
    """审批与超时竞争：节点已推进时，到期升级必须作废而非再次产生待办。"""
    inst, _ = engine.start_instance(db, "leave", "zhang", {"amount": 100}, rid())
    for assignee in ("alice", "bob"):
        t = db.scalar(
            select(Task).where(
                Task.instance_id == inst.id,
                Task.assignee == assignee,
                Task.status == TaskStatus.PENDING,
            )
        )
        engine.decide_task(db, inst.id, t.id, request_id=rid(),
                           expected_version=1, actor=assignee, decision="approve")
    db.expire_all()
    inst = db.get(type(inst), inst.id)
    assert inst.status == InstanceStatus.COMPLETED

    # 模拟审批与超时竞争：节点完成时升级已同步取消。这里手工把升级行重置为
    # pending+到期（模拟“升级在节点推进的同一刻被选中”的防御分支），
    # worker 必须识别出节点已推进并将其作废，而不能再产生待办。
    _force_due(db, inst.id)
    from app.enums import EscalationStatus as _ES

    esc = db.scalar(
        select(Escalation).where(Escalation.instance_id == inst.id)
    )
    esc.status = _ES.PENDING
    esc.processed_at = None
    db.commit()

    assert engine.process_due_escalations(db, limit=10) == 1  # 认领并标记 canceled
    db.expire_all()
    esc = db.scalar(select(Escalation).where(Escalation.instance_id == inst.id))
    assert esc.status == EscalationStatus.CANCELED
    n_tasks = db.scalar(
        select(func.count())
        .select_from(Task)
        .where(Task.instance_id == inst.id, Task.assignee == "frank")
    )
    assert n_tasks == 0


def test_escalation_restart_recovery(db, published_template, rid):
    """模拟服务重启：不依赖内存状态，新会话直接从 escalation 表继续处理。"""
    with SessionLocal() as s:
        inst, _ = engine.start_instance(s, "leave", "wang", {"amount": 100}, rid())
        instance_id = inst.id
        _force_due(s, instance_id)

    # “重启”：用全新会话处理
    with SessionLocal() as s:
        assert engine.process_due_escalations(s, limit=10) == 1
        frank = s.scalar(
            select(Task).where(
                Task.instance_id == instance_id,
                Task.assignee == "frank",
                Task.status == TaskStatus.PENDING,
            )
        )
        assert frank is not None


def test_failed_escalation_retries_next_cycle(db, published_template, rid, monkeypatch):
    """升级处理失败时保留 pending 并记录错误，下一轮重试成功。"""
    inst, _ = engine.start_instance(db, "leave", "zhang", {"amount": 100}, rid())
    _force_due(db, inst.id)

    calls = {"n": 0}
    real_apply = engine._apply_escalation

    def flaky_apply(session, esc):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated worker crash")
        return real_apply(session, esc)

    monkeypatch.setattr(engine, "_apply_escalation", flaky_apply)
    assert engine.process_due_escalations(db, limit=10) == 0

    # 失败信息被记录，仍为 pending
    esc = db.scalar(
        select(Escalation).where(Escalation.instance_id == inst.id)
    )
    assert esc.status == EscalationStatus.PENDING
    assert esc.attempts == 1
    assert "simulated" in (esc.last_error or "")

    # 下一轮成功
    assert engine.process_due_escalations(db, limit=10) == 1
    db.expire_all()
    esc = db.scalar(
        select(Escalation).where(Escalation.instance_id == inst.id)
    )
    assert esc.status == EscalationStatus.DONE
