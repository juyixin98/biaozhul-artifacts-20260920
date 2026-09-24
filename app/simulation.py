"""Discrete-event fixed-priority scheduling reference.

Independent implementation of the same scheduling model assumed by the RTA
module, used purely as a cross-check.  Single core, fully preemptive, exact
integer-tick event simulation:

* Each task releases a job at t = k * T_i for k >= 0 (synchronous release;
  t = 0 is the critical instant).
* The ready job with the strictly highest priority (smallest integer) runs; a
  higher-priority release preempts immediately at its release tick.
* Job absolute deadline = release + D_i.
* Blocking is modeled conservatively to mirror the RTA term B_i: the target
  task's t = 0 job is delayed by a single non-preemptible chunk of length b
  occupying [0, b).  Nothing executes in that interval (not even
  higher-priority releases), which is the worst-case blocking pattern the
  recurrence's B_i stands in for.  b == 0 is a normal preemptive run.

Everything is bounded by explicit release/tick caps.  If a cap is hit the run
is marked truncated and any cross-check built on it is inconclusive, never
guessed.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from math import gcd
from typing import Dict, List, Optional, Tuple

from .models import Task


@dataclass
class _Job:
    task_id: str
    release: int
    deadline: int
    remaining: int
    priority: int
    seq: int


@dataclass
class SimOutcome:
    horizon_ticks: int
    horizon_basis: str  # hyperperiod | capped | released_jobs_cap
    truncated: bool
    released_jobs: int
    completed_jobs: int
    deadline_misses: int
    first_miss: Optional[dict]
    first_job_response_times: Dict[str, int] = field(default_factory=dict)
    t0_completed: Dict[str, bool] = field(default_factory=dict)


def _lcm(a: int, b: int) -> int:
    return a // gcd(a, b) * b


def hyperperiod(tasks: List[Task]) -> int:
    h = 1
    for t in tasks:
        h = _lcm(h, t.period)
    return h


def simulate(
    tasks: List[Task],
    *,
    horizon_ticks: Optional[int] = None,
    blocking_override: Optional[Dict[str, int]] = None,
    tick_cap: int = 2_000_000,
    released_jobs_cap: int = 200_000,
) -> SimOutcome:
    """Fixed-priority preemptive discrete-event simulation.

    With ``horizon_ticks=None`` the window is one hyperperiod; jobs released
    before the window end are drained to completion (bounds permitting).
    """
    override = blocking_override or {}
    by_id = {t.id: t for t in tasks}

    hp = hyperperiod(tasks)
    requested = hp if horizon_ticks is None else horizon_ticks
    basis = "hyperperiod" if horizon_ticks is None else "capped"
    horizon = min(requested, tick_cap)
    truncated = horizon < requested

    # One global release queue: (release_time, seq, task_id).
    release_q: List[Tuple[int, int, str]] = []
    seq = 0
    cap_hit = False
    for t in tasks:
        r = 0
        while r < horizon:
            if len(release_q) >= released_jobs_cap:
                cap_hit = True
                break
            release_q.append((r, seq, t.id))
            seq += 1
            r += t.period
        if cap_hit:
            break
    if cap_hit:
        truncated = True
        basis = "released_jobs_cap"
    heapq.heapify(release_q)

    # Ready heap of unfinished jobs: (priority, seq, job).
    ready: List[Tuple[int, int, _Job]] = []
    completed = 0
    misses = 0
    first_miss: Optional[dict] = None
    first_rt: Dict[str, int] = {}
    t0_done: Dict[str, bool] = {}
    released_count = 0
    now = 0

    def release_all(at: int) -> None:
        nonlocal released_count
        while release_q and release_q[0][0] <= at:
            rel, s, tid = heapq.heappop(release_q)
            t = by_id[tid]
            heapq.heappush(
                ready,
                (
                    t.priority,
                    s,
                    _Job(tid, rel, rel + t.deadline, t.wcet, t.priority, s),
                ),
            )
            released_count += 1

    def note_miss(job: _Job, completion: int) -> None:
        nonlocal misses, first_miss
        misses += 1
        if first_miss is None:
            first_miss = {
                "task_id": job.task_id,
                "release": job.release,
                "deadline": job.deadline,
                "completion": completion,
                "response_time": completion - job.release,
                "lateness": completion - job.deadline,
            }

    # Optional worst-case non-preemptible blocking chunk at the critical
    # instant: nothing runs during [0, b); all releases up to b are flushed to
    # the ready queue but never execute in the interval.
    block = 0
    for tid, b in override.items():
        if b and b > 0:
            block = b
            break
    if block:
        now = min(block, tick_cap)
        if block > tick_cap:
            truncated = True

    release_all(now)

    # Event-driven execution.  Each iteration runs the highest-priority ready
    # job up to the next job release or to its own completion.
    while True:
        chosen_entry: Optional[Tuple[int, int, _Job]] = None
        while ready:
            entry = heapq.heappop(ready)
            if entry[2].remaining > 0:
                chosen_entry = entry
                break

        if chosen_entry is None:
            # CPU idle: jump to the next release inside the window.
            if not release_q or release_q[0][0] >= horizon:
                break
            now = release_q[0][0]
            if now > tick_cap:
                truncated = True
                break
            release_all(now)
            continue

        prio, s, job = chosen_entry

        # A deadline already passed while waiting -> first miss at this instant.
        if now >= job.deadline and job.release == 0:
            # Record only once, at the moment it becomes undeniable; keep
            # simulating so completion-time accounting stays consistent.
            pass

        next_rel = release_q[0][0] if release_q and release_q[0][0] > now else None
        if next_rel is not None:
            run_for = min(job.remaining, next_rel - now)
        else:
            run_for = job.remaining

        job.remaining -= run_for
        now += run_for
        release_all(now)

        if job.remaining == 0:
            completion = now
            completed += 1
            if job.release == 0:
                first_rt[job.task_id] = completion - job.release
                t0_done[job.task_id] = True
            if completion > job.deadline:
                note_miss(job, completion)
            # Job finished; it stays out of the ready heap.
        else:
            # Preempted by a release at `now`; requeue for re-arbitration.
            heapq.heappush(ready, (prio, s, job))

        more_releases = bool(release_q and release_q[0][0] < horizon)
        unfinished_ready = any(
            j.remaining > 0 for _, _, j in ready
        )
        if not more_releases and not unfinished_ready:
            break
        if now > tick_cap:
            truncated = True
            break

    # Any unfinished ready job whose deadline has passed is a miss.
    for _, _, job in ready:
        if job.remaining > 0 and now >= job.deadline:
            note_miss(job, max(now, job.deadline))

    return SimOutcome(
        horizon_ticks=horizon,
        horizon_basis=basis,
        truncated=truncated,
        released_jobs=released_count,
        completed_jobs=completed,
        deadline_misses=misses,
        first_miss=first_miss,
        first_job_response_times=first_rt,
        t0_completed=t0_done,
    )
