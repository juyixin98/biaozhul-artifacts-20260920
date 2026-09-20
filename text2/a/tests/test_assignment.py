"""约束分配：资格覆盖、时间重叠、休息间隔、跨周工时、候选人排序。"""

from datetime import datetime, timedelta, timezone

from conftest import (generate, make_caregiver, make_plan,
                      make_qualification)

from app.models import Assignment, CareTask
from app.services.assignment import split_by_week

UTC = timezone.utc


def _task_ids(client, plan_id):
    r = client.get("/tasks", params={"plan_id": plan_id})
    return [t["id"] for t in r.json()]


def test_split_by_week_midnight_boundary():
    # 周日 22:00 -> 周一 02:00：2h 落在 W 前一周，2h 落在下一周
    start = datetime(2026, 9, 20, 22, 0, tzinfo=UTC)   # 周日
    end = datetime(2026, 9, 21, 2, 0, tzinfo=UTC)      # 周一
    parts = dict(split_by_week(start, end))
    assert parts[(2026, 38)] == 2.0
    assert parts[(2026, 39)] == 2.0


def test_qualification_must_cover_whole_task_window(env):
    client, clock, db = env
    cg = make_caregiver(client)
    # 资格在任务结束前到期 -> 不覆盖整个时段
    make_qualification(client, cg, "RN",
                       valid_from="2026-09-01T00:00:00Z",
                       valid_until="2026-09-14T09:30:00Z")
    plan_id = make_plan(client)
    generate(client, plan_id)
    task_id = _task_ids(client, plan_id)[0]  # 9/14 09:00-10:00

    r = client.post(f"/tasks/{task_id}/offer", json={})
    assert r.status_code == 422
    violations = r.json()["violations"]
    assert violations[0]["caregiver_id"] == cg
    assert any("qualification" in reason for reason in violations[0]["reasons"])


def test_overlap_and_rest_constraints(env):
    client, clock, db = env
    cg = make_caregiver(client)
    make_qualification(client, cg, "RN")
    plan_id = make_plan(client)  # 每天 09:00-10:00 UTC
    generate(client, plan_id)
    task_ids = _task_ids(client, plan_id)

    # 把 9/14 09:00-10:00 分给护理员
    r = client.post(f"/tasks/{task_ids[0]}/offer", json={"caregiver_id": cg})
    offer_id = r.json()["id"]
    client.post(f"/offers/{offer_id}/accept", json={"caregiver_id": cg})

    # 同一天 12:00-13:00 的任务：间隔 2h < 10h -> 休息不足
    other = make_plan(client, name="noon", window_start="12:00:00",
                      window_end="14:00:00", start_date="2026-09-14")
    generate(client, other)
    noon_task = _task_ids(client, other)[0]
    r = client.post(f"/tasks/{noon_task}/offer", json={"caregiver_id": cg})
    assert r.status_code == 422
    assert any("rest gap" in reason
               for reason in r.json()["violations"][0]["reasons"])

    # 重叠任务：09:30-10:30 与 09:00-10:00 重叠
    overlap = make_plan(client, name="overlap", window_start="09:30:00",
                        window_end="11:00:00", duration_minutes=60,
                        start_date="2026-09-14")
    generate(client, overlap)
    overlap_task = _task_ids(client, overlap)[0]
    r = client.post(f"/tasks/{overlap_task}/offer", json={"caregiver_id": cg})
    assert r.status_code == 422
    assert any("overlaps" in reason
               for reason in r.json()["violations"][0]["reasons"])


def _insert_assignment(db, caregiver_id, start, end):
    task = CareTask(plan_id=0, plan_version=1, scheduled_start=start,
                    scheduled_end=end, status="assigned",
                    required_qualification="RN", unit_id="unit-a")
    db.add(task)
    db.flush()
    db.add(Assignment(task_id=task.id, caregiver_id=caregiver_id,
                      source="manual", created_by="test", created_at=start))
    db.flush()


def test_cross_midnight_weekly_hours_limit(env):
    """跨日班次按实际时间拆分到对应周；任一周超过 44h 即拒绝。"""
    client, clock, db = env
    cg = make_caregiver(client)
    make_qualification(client, cg, "RN")

    # W38 (9/14-9/20) 已有 42h：周一到周五 8h + 周六 2h，间隔均 >= 10h
    for day in range(14, 19):  # 9/14..9/18 每天 08:00-16:00
        _insert_assignment(db, cg, datetime(2026, 9, day, 8, tzinfo=UTC),
                           datetime(2026, 9, day, 16, tzinfo=UTC))
    _insert_assignment(db, cg, datetime(2026, 9, 19, 8, tzinfo=UTC),
                       datetime(2026, 9, 19, 10, tzinfo=UTC))
    db.commit()

    # 新任务：周日 9/20 22:00 -> 周一 9/21 02:00（W38 2h + W39 2h）
    plan_id = make_plan(client, window_start="22:00:00",
                        window_end="02:00:00", duration_minutes=240,
                        start_date="2026-09-20")
    generate(client, plan_id)
    task_id = _task_ids(client, plan_id)[0]
    task = db.get(CareTask, task_id)
    assert task.scheduled_start == datetime(2026, 9, 20, 22, tzinfo=UTC)
    assert task.scheduled_end == datetime(2026, 9, 21, 2, tzinfo=UTC)

    # W38 42h + 2h = 44h，恰好不超 -> 允许
    r = client.post(f"/tasks/{task_id}/offer", json={"caregiver_id": cg})
    assert r.status_code == 201, r.text

    # 再插 1h 到 W38（周六 9/19 11:00-12:00，距前后班次 >= 10h? 10:00->11:00 仅 1h）
    # 改为直接再占 W38 一小时且满足休息：周日 9/20 02:00-03:00 距周六 10:00 为 16h
    _insert_assignment(db, cg, datetime(2026, 9, 20, 2, tzinfo=UTC),
                       datetime(2026, 9, 20, 3, tzinfo=UTC))
    db.commit()

    # 现在 W38 已有 43h，再来一个跨日 4h 任务 -> W38 部分 2h -> 45h > 44h
    plan2 = make_plan(client, name="late", window_start="22:00:00",
                      window_end="02:00:00", duration_minutes=240,
                      start_date="2026-09-20")
    generate(client, plan2)
    task2 = _task_ids(client, plan2)[0]
    r = client.post(f"/tasks/{task2}/offer", json={"caregiver_id": cg})
    assert r.status_code == 422
    assert any("weekly hours" in reason and "W38" in reason
               for reason in r.json()["violations"][0]["reasons"])


def test_candidate_ranking_prefers_more_remaining_and_less_load(env):
    client, clock, db = env
    cg_busy = make_caregiver(client, name="busy")
    cg_free = make_caregiver(client, name="free")
    make_qualification(client, cg_busy, "RN")
    make_qualification(client, cg_free, "RN")

    # cg_busy 本周已有 20h 负载
    for day in (14, 15):
        _insert_assignment(db, cg_busy, datetime(2026, 9, day, 8, tzinfo=UTC),
                           datetime(2026, 9, day, 18, tzinfo=UTC))
    db.commit()

    plan_id = make_plan(client, window_start="08:00:00",
                        window_end="12:00:00", duration_minutes=60,
                        start_date="2026-09-16")
    generate(client, plan_id)
    task_id = _task_ids(client, plan_id)[0]

    r = client.post(f"/tasks/{task_id}/offer", json={})
    assert r.status_code == 201
    assert r.json()["caregiver_id"] == cg_free  # 剩余时间多、负载低者优先


def test_no_eligible_caregiver_returns_constraints(env):
    client, clock, db = env
    cg = make_caregiver(client)  # 没有任何资格
    plan_id = make_plan(client)
    generate(client, plan_id)
    task_id = _task_ids(client, plan_id)[0]

    r = client.post(f"/tasks/{task_id}/offer", json={})
    assert r.status_code == 422
    body = r.json()
    assert body["detail"] == "no eligible caregiver"
    assert body["violations"][0]["caregiver_id"] == cg
    assert body["violations"][0]["reasons"]  # 返回未满足的具体约束，不强行排班
    # 任务保持 pending，未被强行分配
    assert client.get(f"/tasks/{task_id}").json()["status"] == "pending"
