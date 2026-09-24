"""离散调度仿真测试：手工轨迹 + 与 RTA 的随机对照。

随机对照只选取超周期不超过 HORIZON_CAP 的小规模任务集，使全局调度
覆盖整数个超周期（每个作业都在 horizon 内释放并有界排空），此时
关键瞬时定理保证：首作业响应 = 最坏响应 = RTA 不动点（可调度时）。
"""

from __future__ import annotations

import random

import pytest

from app.model import TaskInput
from app.rta import analyze_taskset, assign_priorities
from app.simulation import (
    HORIZON_CAP,
    _blocking_response_for,
    _global_schedule,
    _hyperperiod,
    simulate_and_crosscheck,
)


def t(tid, C, T, D=None, B=0):
    return TaskInput(id=tid, C=C, T=T, D=D if D is not None else T, B=B)


def test_global_schedule_simple_trace():
    # 单任务 C=2 T=5，超周期=5，释放窗口 [0,5)：仅 t=0 一个作业
    jobs, horizon, misses, backlog = _global_schedule([t("a", 2, 5)])
    assert horizon == 5
    assert misses == 0 and backlog == 0
    assert [(j.release, j.start, j.finish) for j in jobs] == [(0, 0, 2)]


def test_global_schedule_preemption_trace():
    # lo: C=3 T=10；hi: C=1 T=4（都在 0 释放）
    # 调度: [0,1) hi, [1,4) lo（恰在 4 完成）, [4,5) hi(第二次),
    # [5,8) hi(第三次), [8,9) ... lo 下一作业在 10 释放
    # → lo 首作业 start=1 finish=4 response=4（= RTA 不动点）
    jobs, horizon, _, backlog = _global_schedule([t("lo", 3, 10), t("hi", 1, 4)])
    assert horizon == 20 and backlog == 0
    lo0 = next(j for j in jobs if j.task_id == "lo" and j.release == 0)
    assert (lo0.start, lo0.finish, lo0.response) == (1, 4, 4)
    assert lo0.deadline_missed is False
    hi0 = next(j for j in jobs if j.task_id == "hi" and j.release == 0)
    assert (hi0.start, hi0.finish) == (0, 1)
    # 抢占确实发生过：lo 在 10 释放的第二作业运行 [10,12)，
    # 被 12 释放的 hi 抢占（[12,13)），lo 在 [13,14) 完成
    lo1 = next(j for j in jobs if j.task_id == "lo" and j.release == 10)
    assert (lo1.start, lo1.finish) == (10, 14)


def test_global_schedule_reports_miss_and_backlog():
    # U>1：fast C=2 T=3, slow C=3 T=4 → horizon=12 处 slow 有作业未完成
    jobs, horizon, misses, backlog = _global_schedule(
        [t("fast", 2, 3), t("slow", 3, 4)]
    )
    assert horizon == 12
    assert misses >= 1
    assert backlog >= 1
    assert any(j.deadline_missed for j in jobs)


def test_blocking_emulation_matches_hand_trace():
    # target lo: C=2 T=10 B=3；hp hi: C=1 T=4
    # 持锁者继承 lo 的优先级（hp 可抢占，lo 必须等待）:
    # [0,1) hi; [1,3) holder 跑 2; [4,5) hi; [5,6) holder 完成;
    # [6,7) lo 跑 1; [8,9) hi; [9,10) lo 完成 → response=7（= RTA 不动点）
    ordered = assign_priorities([t("hi", 1, 4), t("lo", 2, 10, B=3)])
    idx = next(i for i, o in enumerate(ordered) if o.task.id == "lo")
    r, status = _blocking_response_for(ordered, idx)
    assert status == "completed"
    assert r == 7


def test_crosscheck_matches_on_known_sets():
    for ts in [
        [t("a", 1, 4), t("b", 2, 6, B=1), t("c", 1, 8, B=1)],
        [t("a", 1, 4), t("b", 2, 6), t("c", 3, 8)],
        [t("fast", 2, 3), t("slow", 3, 4)],
        [t("x", 1, 3, D=2), t("y", 2, 7, B=2)],
    ]:
        report = simulate_and_crosscheck(ts, analyze_taskset(ts))
        assert report.matches_rta, report.mismatch


def _random_taskset(rng: random.Random) -> list[TaskInput]:
    n = rng.randint(1, 4)
    periods = rng.sample(range(3, 25), k=n)  # 保证周期互异，简化可读性
    periods.sort()
    tasks: list[TaskInput] = []
    for i, per in enumerate(periods):
        c = rng.randint(1, max(1, per // 2))
        d = rng.randint(c, per)  # D in [C, T]
        b = rng.choice([0, 0, 0, rng.randint(0, max(0, d - c))])
        tasks.append(t(f"t{i}", c, per, d, b))
    return tasks


def test_randomized_agreement_rta_vs_discrete_sim():
    rng = random.Random(20260923)
    trials = 0
    max_trials = 300
    attempts = 0
    while trials < max_trials and attempts < 3000:
        attempts += 1
        ts = _random_taskset(rng)
        if _hyperperiod([x.period for x in ts]) >= HORIZON_CAP:
            continue
        trials += 1
        rta = analyze_taskset(ts)
        report = simulate_and_crosscheck(ts, rta)
        assert report.matches_rta, f"mismatch={report.mismatch}; set={ts}"

        # 对可调度任务，全局无阻塞调度的首作业响应也必须等于 RTA(B=0 的部分)
        if all(x.blocking == 0 for x in ts):
            global_jobs, _, misses, backlog = _global_schedule(ts)
            overall = all(r.schedulable for r in rta)
            if overall:
                assert misses == 0 and backlog == 0
                first_resp = {}
                for j in global_jobs:
                    first_resp.setdefault(j.task_id, j.response)
                for r in rta:
                    assert first_resp[r.task_id] == r.final_rt
            else:
                # 不可调度且无阻塞：全局调度必须观察到错失
                assert misses >= 1
    assert trials >= 100, f"有效随机试验过少: {trials}"
