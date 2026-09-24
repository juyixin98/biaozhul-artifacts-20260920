"""Response-Time Analysis (RTA) for fixed-priority periodic tasks.

Recurrence (Joseph & Pandya, 1986), with a caller-supplied blocking term:

    R_i^(0)  = C_i + B_i
    R_i^(n+1) = C_i + B_i + sum_{j in hp(i)} ceil(R_i^n / T_j) * C_j

where hp(i) is the set of strictly higher-priority tasks (smaller priority
integer).  The recurrence is non-decreasing.  Termination:

* fixed point (R^(n+1) == R^n) and R^n <= D_i  -> schedulable
* candidate exceeds D_i                          -> deadline miss (fail fast)
* candidate exceeds the configured value guard  -> overflow, not a success
* iteration guard hit without a fixed point      -> no convergence, not a success

Overflow / non-convergence are never reported as schedulable.  The arithmetic
is exact integer arithmetic; there is no floating point in the decision path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .models import Task

# Terminal condition labels.
FIXED_POINT = "fixed_point"
DEADLINE_EXCEEDED = "deadline_exceeded"
ITERATION_LIMIT = "iteration_limit"
VALUE_OVERFLOW = "value_overflow"


def hp_tasks(all_tasks: List[Task], target: Task) -> List[Task]:
    """Strictly higher-priority tasks (smaller priority integer), lowest index first."""
    return sorted(
        (t for t in all_tasks if t.priority < target.priority),
        key=lambda t: (t.priority, t.id),
    )


def interference_at(r: int, higher: List[Task]) -> Dict[str, int]:
    """ceil(r / T_j) * C_j contributed by each higher-priority task at time r.

    Integer-only ceiling division (no floating point); ceil(0 / T) == 0.
    """
    contrib: Dict[str, int] = {}
    for t in higher:
        count = (r + t.period - 1) // t.period
        contrib[t.id] = count * t.wcet
    return contrib


@dataclass
class RTAResult:
    task_id: str
    schedulable: bool
    terminal_condition: str
    iterations: List[Dict[str, Any]] = field(default_factory=list)
    response_time: Optional[int] = None
    failed_deadline: Optional[int] = None
    failure_detail: Optional[str] = None
    # Diagnostic fixed point continued past a deadline miss (may be None).
    continued_fixed_point: Optional[int] = None
    higher_priority: List[str] = field(default_factory=list)


def rta_for_task(
    target: Task,
    all_tasks: List[Task],
    max_iterations: int = 10_000,
    max_response_ticks: int = 10**12,
) -> RTAResult:
    """Run the response-time recurrence for one task.

    Fails fast on the first candidate that exceeds the deadline, then
    (diagnostically) continues the iteration to a fixed point under the same
    guards so the report can show the actual worst-case response time even
    though schedulability has already been decided as False.
    """
    higher = hp_tasks(all_tasks, target)
    result = RTAResult(
        task_id=target.id,
        schedulable=False,
        terminal_condition=ITERATION_LIMIT,
        higher_priority=[t.id for t in higher],
    )

    c, b, d = target.wcet, target.blocking, target.deadline
    base = c + b

    def step(n: int, r: int, record: bool) -> Optional[int]:
        contrib = interference_at(r, higher)
        total = sum(contrib.values())
        nxt = base + total
        if record:
            result.iterations.append(
                {
                    "n": n,
                    "r": r,
                    "interference": dict(contrib),
                    "total_interference": total,
                    "next_r": nxt,
                }
            )
        return nxt

    # R_0 = C_i + B_i  (recorded as step n=0).
    r = base
    deadline_missed_at: Optional[int] = None

    for n in range(0, max_iterations):
        nxt = step(n, r, record=True)

        if nxt > max_response_ticks:
            result.terminal_condition = VALUE_OVERFLOW
            result.failure_detail = (
                f"response-time candidate R_{n + 1}={nxt} exceeded the configured "
                f"safety bound of {max_response_ticks} ticks; treated as overflow, not success"
            )
            # Fix the "next_r" pointer on the last recorded step.
            return result

        # Deadline violation is checked before the fixed-point test so that a
        # task whose initial R_0 = C_i + B_i already exceeds D_i is still
        # reported as a deadline miss (it may simultaneously be a fixed point).
        if deadline_missed_at is None and nxt > d:
            deadline_missed_at = n + 1
            result.failed_deadline = d
            result.failure_detail = (
                f"R_{n + 1}={nxt} exceeded relative deadline D={d} "
                f"(first violation at iteration {n + 1})"
            )

        if nxt == r:
            result.response_time = r
            result.iterations[-1]["next_r"] = r  # fixed point maps to itself
            if deadline_missed_at is not None:
                result.terminal_condition = DEADLINE_EXCEEDED
                result.continued_fixed_point = r
                result.schedulable = False
            else:
                result.terminal_condition = FIXED_POINT
                result.schedulable = True
            return result

        r = nxt

    # Guard exhausted without a fixed point -> did not converge.
    result.terminal_condition = (
        DEADLINE_EXCEEDED if deadline_missed_at is not None else ITERATION_LIMIT
    )
    if deadline_missed_at is None:
        result.failure_detail = (
            f"RTA did not converge within {max_iterations} iterations; "
            "treated as non-convergent, not success"
        )
    return result


def run_rta(
    tasks: List[Task],
    max_iterations: int = 10_000,
    max_response_ticks: int = 10**12,
) -> List[RTAResult]:
    """Analyze every task.  Priority order is irrelevant to correctness;
    we iterate highest priority first for readable output."""
    ordered = sorted(tasks, key=lambda t: (t.priority, t.id))
    return [
        rta_for_task(t, tasks, max_iterations, max_response_ticks)
        for t in ordered
    ]
