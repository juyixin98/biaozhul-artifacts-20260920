"""RTA 核心单元测试：手工核算的可调度/不可调度集合、优先级与迭代语义。"""

from __future__ import annotations

import pytest

from app.model import TaskInput
from app.rta import analyze_taskset, assign_priorities


def t(tid: str, C: int, T: int, D: int | None = None, B: int = 0) -> TaskInput:
    return TaskInput(id=tid, C=C, T=T, D=D if D is not None else T, B=B)


def by_id(results, tid):
    return next(r for r in results if r.task_id == tid)


def test_single_task_no_interference():
    [r] = analyze_taskset([t("a", C=3, T=10)])
    assert r.schedulable
    assert r.outcome == "converged"
    assert r.final_rt == 3
    assert [s.step for s in r.iterations] == [0, 1]
    assert r.iterations[1].r_prev == 3 and r.iterations[1].r_next == 3


def test_single_task_blocking_within_deadline():
    [r] = analyze_taskset([t("a", C=3, T=10, B=2)])
    assert r.schedulable
    assert r.final_rt == 5  # R0 = C+B = 5，无干扰即不动点


def test_single_task_cb_exceeds_deadline():
    # C+B = 6 > D=5：第 0 步即失败，deadline_miss = 1
    [r] = analyze_taskset([t("a", C=3, T=10, D=5, B=3)])
    assert not r.schedulable
    assert r.outcome == "deadline_miss"
    assert r.final_rt == 6
    assert r.deadline_miss == 1
    assert len(r.iterations) == 1  # 只有初值步


def test_rm_priorities_shorter_period_higher_rank():
    ts = [t("lo", C=1, T=10), t("hi", C=1, T=2), t("mid", C=1, T=5)]
    ordered = assign_priorities(ts)
    assert [o.task.id for o in ordered] == ["hi", "mid", "lo"]
    assert [o.rank for o in ordered] == [1, 2, 3]


def test_rm_tie_break_is_input_order():
    ts = [t("first", C=1, T=5), t("second", C=1, T=5)]
    ordered = assign_priorities(ts)
    assert [o.task.id for o in ordered] == ["first", "second"]
    assert by_id(analyze_taskset(ts), "second").higher_priority_tasks == ["first"]


def test_schedulable_example_hand_computed():
    # tau1: C1 T4；tau2: C2 T6 B1；tau3: C1 T8 B1
    ts = [t("tau1", 1, 4, B=0), t("tau2", 2, 6, B=1), t("tau3", 1, 8, B=1)]
    res = analyze_taskset(ts)
    assert all(r.schedulable for r in res)
    r2, r3 = by_id(res, "tau2"), by_id(res, "tau3")
    # tau2: R0=3 → 3+ceil(3/4)=4 → 4+ceil(4/4)=4 不动点
    assert [s.r_next for s in r2.iterations] == [3, 4, 4]
    assert r2.final_rt == 4
    # tau3: R0=C+B=2
    #   R1 = 2 + ceil(2/4)*1 + ceil(2/6)*2 = 2+1+2 = 5
    #   R2 = 2 + ceil(5/4)  + ceil(5/6)*2  = 2+2+2 = 6
    #   R3 = 2 + ceil(6/4)  + ceil(6/6)*2  = 6（不动点，<=8）
    assert [s.r_next for s in r3.iterations] == [2, 5, 6, 6]
    assert r3.final_rt == 6


def test_unschedulable_util_under_one():
    # U = 1/4 + 2/6 + 3/8 = 23/24 < 1，但 tau3 错失：低利用率不充分
    ts = [t("tau1", 1, 4), t("tau2", 2, 6), t("tau3", 3, 8)]
    res = analyze_taskset(ts)
    assert by_id(res, "tau1").schedulable
    assert by_id(res, "tau2").schedulable
    r3 = by_id(res, "tau3")
    assert not r3.schedulable
    assert r3.outcome == "deadline_miss"
    assert r3.final_rt > 8
    assert r3.deadline_miss == r3.final_rt - 8
    # 最后一个记录的迭代步就是首次越界步；相邻步以 r_prev→r_next 衔接
    assert r3.iterations[-1].r_next == r3.final_rt
    penultimate, last = r3.iterations[-2], r3.iterations[-1]
    assert last.r_prev == penultimate.r_next


def test_overload_u_gt_one():
    # U = 2/3 + 3/4 > 1
    ts = [t("fast", 2, 3), t("slow", 3, 4)]
    res = analyze_taskset(ts)
    slow = by_id(res, "slow")
    assert not slow.schedulable
    assert slow.outcome == "deadline_miss"
    assert slow.final_rt > 4


def test_interference_sources_listed_per_step():
    ts = [t("hi", 1, 3), t("lo", 2, 7)]
    lo = by_id(analyze_taskset(ts), "lo")
    for step in lo.iterations[1:]:
        ids = [i.task_id for i in step.interferences]
        assert ids == ["hi"]
        i0 = step.interferences[0]
        assert i0.releases_in_last_rt == -(-step.r_prev // 3)
        assert i0.interference == i0.releases_in_last_rt * i0.wcet


def test_iteration_sequence_monotone_and_terminates():
    ts = [t("a", 1, 7), t("b", 1, 9), t("c", 2, 13), t("d", 3, 20)]
    for r in analyze_taskset(ts):
        seq = [s.r_next for s in r.iterations]
        assert all(seq[i] <= seq[i + 1] for i in range(len(seq) - 1))
        assert r.outcome in {"converged", "deadline_miss", "non_converged"}
