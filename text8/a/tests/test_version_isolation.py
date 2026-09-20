"""版本不可变与版本隔离测试。"""
from __future__ import annotations

import pytest

from app import engine
from app.enums import InstanceStatus, VersionStatus
from app.errors import ConflictError


def test_published_version_is_immutable(db, simple_definition):
    engine.create_template(db, "leave", "请假", simple_definition)
    engine.publish_version(db, "leave", 1)
    v1 = engine._get_version(db, engine._get_template(db, "leave").id, 1)
    assert v1.status == VersionStatus.PUBLISHED
    # 定义内容不得再被修改：ORM 层没有更新接口；直接改属性也应视为发布快照。
    # 新增版本而不是覆盖。
    new_def = {
        "start_node": "start",
        "nodes": [
            {"id": "start", "type": "start", "next": "end"},
            {"id": "end", "type": "end"},
        ],
    }
    v2 = engine.add_version(db, engine._get_template(db, "leave"), new_def)
    engine.publish_version(db, "leave", 2)
    assert v1.version == 1 and v2.version == 2
    # v1 的定义保持原样
    assert [n["id"] for n in v1.definition["nodes"]] == [
        "start", "dept", "check", "gm", "end"
    ]


def test_running_instance_keeps_old_version_after_publish(db, published_template, rid):
    # 实例 1：v1
    inst1, replay = engine.start_instance(
        db, "leave", "zhang", {"amount": 100}, rid()
    )
    assert replay is None
    assert inst1.version_number == 1
    assert inst1.current_node_id == "dept"

    # 发布 v2：start 直达 end
    new_def = {
        "start_node": "start",
        "nodes": [
            {"id": "start", "type": "start", "next": "end"},
            {"id": "end", "type": "end"},
        ],
    }
    engine.add_version(db, engine._get_template(db, "leave"), new_def)
    engine.publish_version(db, "leave", 2)

    # 新实例绑定 v2，直接完成
    inst2, _ = engine.start_instance(db, "leave", "li", {}, rid())
    assert inst2.version_number == 2
    assert inst2.status == InstanceStatus.COMPLETED

    # 老实例仍在 v1 的 dept 节点，按 v1 继续执行
    db.refresh(inst1)
    assert inst1.version_number == 1
    assert inst1.current_node_id == "dept"


def test_rollback_only_affects_new_instances(db, published_template, rid):
    v1_def = engine._get_template(db, "leave").current_version.definition

    inst1, _ = engine.start_instance(db, "leave", "zhang", {"amount": 100}, rid())

    new_def = {
        "start_node": "start",
        "nodes": [
            {"id": "start", "type": "start", "next": "end"},
            {"id": "end", "type": "end"},
        ],
    }
    engine.add_version(db, engine._get_template(db, "leave"), new_def)
    engine.publish_version(db, "leave", 2)
    # 回滚到 v1
    engine.rollback_template(db, "leave", 1)

    inst2, _ = engine.start_instance(db, "leave", "li", {"amount": 500}, rid())
    assert inst2.version_number == 1
    assert inst2.current_node_id == "dept"  # v1 有审批节点

    db.refresh(inst1)
    assert inst1.version_number == 1
    assert inst1.current_node_id == "dept"
    # v1 定义对象内容未变
    assert v1_def["start_node"] == "start"


def test_expected_version_conflict_rejected(db, published_template, rid):
    inst, _ = engine.start_instance(db, "leave", "zhang", {"amount": 100}, rid())
    tasks = engine._pending_tasks if hasattr(engine, "_pending_tasks") else None
    from app.models import Task
    from sqlalchemy import select
    task = db.scalar(select(Task).where(Task.instance_id == inst.id))

    # 用错误的预期版本操作：409 且任务仍 pending
    with pytest.raises(ConflictError):
        engine.decide_task(
            db, inst.id, task.id, request_id=rid(), expected_version=99,
            actor="alice", decision="approve", comment=None,
        )
    db.refresh(task)
    assert task.status == "pending"
