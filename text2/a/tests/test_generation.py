"""任务生成：14 天窗口、幂等、改版只影响未开始任务、前置任务、时区。"""

from datetime import datetime, timezone

from conftest import generate, make_caregiver, make_plan, make_qualification

from app.models import CareTask


def _tasks(db, plan_id):
    return (db.query(CareTask).filter(CareTask.plan_id == plan_id)
            .order_by(CareTask.scheduled_start).all())


def test_generate_daily_plan_covers_14_days(env):
    client, clock, db = env
    plan_id = make_plan(client)
    result = generate(client, plan_id)
    # 2026-09-14 09:00 UTC 尚未到（now=08:00），含今天共 15 个 occurrence
    assert result["created"] == 15
    tasks = _tasks(db, plan_id)
    assert tasks[0].scheduled_start == datetime(2026, 9, 14, 9, 0,
                                                tzinfo=timezone.utc)
    assert tasks[-1].scheduled_start == datetime(2026, 9, 28, 9, 0,
                                                 tzinfo=timezone.utc)


def test_generate_is_idempotent(env):
    client, clock, db = env
    plan_id = make_plan(client)
    first = generate(client, plan_id)
    second = generate(client, plan_id)
    assert first["created"] == 15
    assert second["created"] == 0  # 重复生成不重复建单
    assert db.query(CareTask).count() == 15


def test_generate_weekly_byweekday(env):
    client, clock, db = env
    plan_id = make_plan(client, frequency="weekly", byweekday=["MO", "WE"])
    result = generate(client, plan_id)
    # 窗口内周一/周三：9/14,16,21,23,28 共 5 次
    assert result["created"] == 5
    weekdays = {t.scheduled_start.weekday() for t in _tasks(db, plan_id)}
    assert weekdays == {0, 2}


def test_generate_respects_service_timezone(env):
    client, clock, db = env
    plan_id = make_plan(client, timezone="Asia/Shanghai",
                        window_start="09:00:00")
    generate(client, plan_id)
    task = _tasks(db, plan_id)[0]
    # 上海 09:00 = UTC 01:00；now=08:00 UTC，故今天的已过期，首个是明天
    assert task.scheduled_start == datetime(2026, 9, 15, 1, 0,
                                            tzinfo=timezone.utc)


def test_revision_only_affects_unstarted_tasks(env):
    client, clock, db = env
    plan_id = make_plan(client)
    generate(client, plan_id)

    clock.advance(days=3)  # 到 9/17 08:00：9/14-9/17 的任务已开始
    r = client.put(f"/plans/{plan_id}", json={"duration_minutes": 90})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["version"] == 2

    tasks = _tasks(db, plan_id)
    started = [t for t in tasks if t.scheduled_start <= clock.now()]
    future_v1 = [t for t in tasks
                 if t.plan_version == 1 and t.scheduled_start > clock.now()]
    future_v2 = [t for t in tasks if t.plan_version == 2]

    # 已开始的任务保持 v1 且未被取消
    assert started and all(t.plan_version == 1 and t.status == "pending"
                           for t in started)
    # 未开始的 v1 任务全部取消
    assert future_v1 and all(t.status == "cancelled" for t in future_v1)
    # 新版本重新生成且时长为 90 分钟
    assert future_v2 and all(
        (t.scheduled_end - t.scheduled_start).total_seconds() == 5400
        for t in future_v2)

    # 改版后再次生成仍然幂等
    assert generate(client, plan_id)["created"] == 0


def test_prerequisite_task_blocks_offer_until_completed(env):
    client, clock, db = env
    cg = make_caregiver(client)
    make_qualification(client, cg, "RN")

    plan_a = make_plan(client, name="prep")
    plan_b = make_plan(client, name="main", window_start="20:00:00",
                       window_end="22:00:00", prerequisite_plan_id=plan_a)
    generate(client, plan_a)
    generate(client, plan_b)

    task_b = _tasks(db, plan_b)[0]
    assert task_b.depends_on_task_id is not None

    # 前置任务未完成 -> 422，返回具体约束
    r = client.post(f"/tasks/{task_b.id}/offer", json={})
    assert r.status_code == 422
    assert "prerequisite" in str(r.json()["violations"])

    # 完成前置任务（先直接分配再完成）
    dep_id = task_b.depends_on_task_id
    r = client.post(f"/tasks/{dep_id}/offer", json={})
    offer_id = r.json()["id"]
    client.post(f"/offers/{offer_id}/accept", json={"caregiver_id": cg})
    r = client.post(f"/tasks/{dep_id}/complete")
    assert r.status_code == 200

    r = client.post(f"/tasks/{task_b.id}/offer", json={})
    assert r.status_code == 201, r.text
