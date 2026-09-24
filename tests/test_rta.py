"""Unit tests for the response-time recurrence itself."""

import pytest

from app.models import Task
from app.rta import (
    DEADLINE_EXCEEDED,
    FIXED_POINT,
    ITERATION_LIMIT,
    VALUE_OVERFLOW,
    interference_at,
    rta_for_task,
    run_rta,
)


def mk(id, c, t, d, p, b=0):
    return Task(id=id, wcet=c, period=t, deadline=d, blocking=b, priority=p)


class TestHandComputedIterations:
    def test_single_task_r0_equals_c_plus_b(self):
        t = mk("S", c=3, t=10, d=5, p=1)
        r = rta_for_task(t, [t])
        assert r.terminal_condition == FIXED_POINT
        assert r.response_time == 3
        # R_0 = C+B = 3 and the fixed point maps to itself.
        assert r.iterations[0]["n"] == 0
        assert r.iterations[0]["r"] == 3
        assert r.iterations[0]["next_r"] == 3

    def test_three_task_schedulable_trace(self):
        t1 = mk("T1", 1, 4, 3, 1)
        t2 = mk("T2", 2, 8, 8, 2, b=1)
        t3 = mk("T3", 3, 12, 12, 3)
        results = {r.task_id: r for r in run_rta([t1, t2, t3])}

        # T2 (B=1): R0 = C+B = 3
        #   R1 = 3 + ceil(3/4)*1 = 4
        #   R2 = 4 + ceil(4/4)*1 = 5? No: at R=4, ceil(4/4)=1 -> next = 4. Fixed.
        r2 = results["T2"]
        assert r2.response_time == 4
        seq = [(s["r"], s["next_r"]) for s in r2.iterations]
        assert seq == [(3, 4), (4, 4)]
        assert r2.schedulable and r2.response_time <= t2.deadline

        # T3: R0 = 3
        #   R1 = 3 + ceil(3/4)*1 + ceil(3/8)*2 = 3 + 1 + 2 = 6
        #   R2 = 3 + ceil(6/4)*1 + ceil(6/8)*2 = 3 + 2 + 2 = 7
        #   R3 = 3 + ceil(7/4)*1 + ceil(7/8)*2 = 3 + 2 + 2 = 7 (fixed)
        r3 = results["T3"]
        assert r3.response_time == 7
        assert r3.iterations[1]["interference"] == {"T1": 2, "T2": 2}
        assert [(s["r"], s["next_r"]) for s in r3.iterations] == [
            (3, 6), (6, 7), (7, 7)
        ]

    def test_low_utilization_deadline_miss(self):
        t1 = mk("T1", 1, 3, 3, 1)
        t2 = mk("T2", 2, 7, 2, 2)  # U total 0.619
        r2 = rta_for_task(t2, [t1, t2])
        assert r2.schedulable is False
        assert r2.terminal_condition == DEADLINE_EXCEEDED
        assert r2.failed_deadline == 2
        # R0=2 (==D, still ok), R1 = 2 + ceil(2/3)=3 > 2
        assert r2.iterations[0]["r"] == 2
        assert r2.iterations[0]["next_r"] == 3
        assert r2.failure_detail and "exceeded relative deadline" in r2.failure_detail
        # continued fixed point is 3 here.
        assert r2.continued_fixed_point == 3

    def test_blocking_miss(self):
        t1 = mk("T1", 2, 10, 10, 1)
        t2 = mk("T2", 2, 5, 5, 2, b=3)
        r = rta_for_task(t2, [t1, t2])
        assert r.schedulable is False
        assert r.terminal_condition == DEADLINE_EXCEEDED
        # R0 = C+B = 5; R1 = 5 + ceil(5/10)*2 = 7 > 5
        assert r.iterations[0]["r"] == 5
        assert r.iterations[0]["next_r"] == 7
        assert r.failed_deadline == 5

    def test_interference_contributions_exact(self):
        t1 = mk("T1", 2, 6, 6, 1)
        # Recurrence uses ceil(r/T): for any r in (0,6], one hp job interferes.
        assert interference_at(5, [t1]) == {"T1": 2}
        assert interference_at(6, [t1]) == {"T1": 2}
        assert interference_at(0, [t1]) == {"T1": 0}
        assert interference_at(7, [t1]) == {"T1": 4}


class TestGuardsNeverReportSuccess:
    def test_iteration_limit_is_not_success(self):
        # A set whose fixed point needs more than 1 iteration; with the guard
        # pinned to 1 it must be reported non-convergent and NOT schedulable.
        t1 = mk("T1", 1, 4, 4, 1)
        t2 = mk("T2", 2, 7, 7, 2)
        r = rta_for_task(t2, [t1, t2], max_iterations=1)
        assert r.terminal_condition == ITERATION_LIMIT
        assert r.schedulable is False
        assert "did not converge" in r.failure_detail

    def test_value_overflow_is_not_success(self):
        # Pin the magnitude guard below the initial candidate R_0 = C+B = 2:
        # the very first candidate overflows, so never a success.
        t1 = mk("T1", 1, 4, 4, 1)
        t2 = mk("T2", 2, 8, 8, 2)  # actual WCRT = 4
        r = rta_for_task(t2, [t1, t2], max_response_ticks=1)
        assert r.terminal_condition == VALUE_OVERFLOW
        assert r.schedulable is False
        assert r.response_time is None

    def test_deadline_miss_dominates_iteration_guard(self):
        t1 = mk("T1", 60, 100, 100, 1)
        t2 = mk("T2", 100, 200, 150, 2)  # R1=160 > 150
        r = rta_for_task(t2, [t1, t2], max_iterations=2)
        assert r.schedulable is False
        assert r.terminal_condition == DEADLINE_EXCEEDED
        assert r.failed_deadline == 150


class TestEdgeCases:
    def test_zero_wcet_is_schedulable(self):
        t = mk("Z", 0, 5, 5, 1)
        r = rta_for_task(t, [t])
        assert r.terminal_condition == FIXED_POINT
        assert r.response_time == 0
        assert r.schedulable is True

    def test_c_greater_than_deadline_fails(self):
        t = mk("X", 4, 10, 3, 1)
        r = rta_for_task(t, [t])
        assert r.schedulable is False
        assert r.terminal_condition == DEADLINE_EXCEEDED
        assert r.failed_deadline == 3

    def test_monotone_non_decreasing(self):
        t1 = mk("T1", 3, 11, 11, 1)
        t2 = mk("T2", 4, 19, 19, 2)
        r = rta_for_task(t2, [t1, t2])
        vals = [s["next_r"] for s in r.iterations]
        assert all(b >= a for a, b in zip(vals, vals[1:]))
