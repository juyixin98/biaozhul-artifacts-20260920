"""High-level analysis orchestration: RTA + discrete-simulation cross-check."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional

from .models import AnalysisRequest, Task
from .rta import (
    DEADLINE_EXCEEDED,
    FIXED_POINT,
    hp_tasks,
    run_rta,
)
from .simulation import hyperperiod, simulate

MODEL_ASSUMPTIONS = [
    "single preemptive processor core",
    "independent periodic tasks (no precedence constraints; no shared-resource locking analyzed)",
    "synchronous task release at t=0 (critical instant); fixed priorities",
    "smaller priority integer denotes higher priority; priorities are unique (equal priorities rejected)",
    "constrained deadlines D_i <= T_i",
    "blocking is a caller-supplied conservative upper bound B_i on lower-priority interference",
    "all quantities are non-negative integers in discrete time ticks",
]


def canonical_request_json(req: AnalysisRequest) -> bytes:
    """Deterministic JSON encoding (sort keys, no whitespace) of the request."""
    return json.dumps(
        req.model_dump(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def request_digest(req: AnalysisRequest) -> Dict[str, Any]:
    """Real SHA-256 over the canonical, default-normalized request payload."""
    payload = canonical_request_json(req)
    return {
        "alg": "SHA-256",
        "canonical_encoding": "json:sort_keys,separators=(',',':')",
        "sha256": hashlib.sha256(payload).hexdigest(),
        "canonical_payload": json.loads(payload.decode("utf-8")),
        "canonical_payload_bytes": len(payload),
        "note": (
            "Hash is over the Pydantic-normalized request (default options "
            "included). Recompute over 'canonical_payload' to verify."
        ),
    }


def _interference_sources(task: Task, all_tasks: List[Task]) -> List[Dict[str, Any]]:
    sources = []
    for h in hp_tasks(all_tasks, task):
        sources.append(
            {
                "task_id": h.id,
                "priority": h.priority,
                "period": h.period,
                "wcet": h.wcet,
                "contribution_form": f"ceil(R_{task.id} / {h.period}) * {h.wcet}",
            }
        )
    return sources


def analyze(req: AnalysisRequest) -> Dict[str, Any]:
    tasks = req.tasks
    opts = req.options

    rta_results = run_rta(
        tasks,
        max_iterations=opts.max_iterations,
        max_response_ticks=opts.max_response_ticks,
    )
    rta_by_id = {r.task_id: r for r in rta_results}

    # --- Discrete-event reference -----------------------------------------
    sim_payload: Dict[str, Any]
    if not opts.run_simulation:
        sim_payload = {
            "enabled": False,
            "horizon_ticks": 0,
            "horizon_basis": "skipped",
            "truncated": False,
            "released_jobs": 0,
            "completed_jobs": 0,
            "deadline_misses": 0,
            "first_miss": None,
            "first_job_response_times": {},
            "baseline_zero_blocking": None,
            "crosscheck": "skipped",
            "crosscheck_detail": "simulation disabled by request options",
        }
    else:
        baseline = simulate(tasks, blocking_override={})  # B = 0 reference
        base_rt = baseline.first_job_response_times

        overall = "agree"
        details: List[str] = []

        # 1) Pure preemptive baseline: every converged zero-blocking RTA value
        # must equal the simulated t=0 response.
        for r in rta_results:
            task = next(t for t in tasks if t.id == r.task_id)
            if task.blocking != 0:
                continue
            target = (
                r.response_time
                if r.terminal_condition == FIXED_POINT
                else r.continued_fixed_point
                if r.terminal_condition == DEADLINE_EXCEEDED
                else None
            )
            if target is None:
                continue
            observed = base_rt.get(r.task_id)
            if observed is None:
                overall = "inconclusive"
                details.append(f"{r.task_id}: t=0 job not completed within sim bounds")
            elif observed != target:
                overall = "disagree"
                details.append(
                    f"{r.task_id}: sim t=0 response {observed} != RTA {target}"
                )

        # 2) Blocking-aware per-task simulations reproduce B_i for tasks that
        # supply a blocking bound.
        per_task: Dict[str, str] = {}
        for r in rta_results:
            task = next(t for t in tasks if t.id == r.task_id)
            if task.blocking == 0:
                continue
            target = (
                r.response_time
                if r.terminal_condition == FIXED_POINT
                else r.continued_fixed_point
                if r.terminal_condition == DEADLINE_EXCEEDED
                else None
            )
            if target is None:
                per_task[task.id] = "inconclusive"
                continue
            hp = hyperperiod(tasks)
            sim = simulate(
                tasks,
                horizon_ticks=min(hp, 2_000_000),
                blocking_override={task.id: task.blocking},
            )
            observed = sim.first_job_response_times.get(task.id)
            if observed is None:
                per_task[task.id] = "inconclusive"
                if overall == "agree":
                    overall = "inconclusive"
                details.append(
                    f"{task.id}: blocking sim t=0 job not completed within sim bounds"
                )
            elif observed != target:
                per_task[task.id] = "disagree"
                overall = "disagree"
                details.append(
                    f"{task.id}: blocking sim t=0 response {observed} != RTA {target} (B={task.blocking})"
                )
            else:
                per_task[task.id] = "agree"

        sim_payload = {
            "enabled": True,
            "horizon_ticks": baseline.horizon_ticks,
            "horizon_basis": baseline.horizon_basis,
            "truncated": baseline.truncated,
            "released_jobs": baseline.released_jobs,
            "completed_jobs": baseline.completed_jobs,
            "deadline_misses": baseline.deadline_misses,
            "first_miss": baseline.first_miss,
            "first_job_response_times": base_rt,
            "baseline_zero_blocking": {
                "note": "All B_i forced to 0; pure fixed-priority preemptive schedule over one hyperperiod.",
                "hyperperiod": hyperperiod(tasks),
                "deadline_misses": baseline.deadline_misses,
                "first_miss": baseline.first_miss,
                "truncated": baseline.truncated,
            },
            "crosscheck": overall,
            "crosscheck_detail": "; ".join(details) if details else "all checked tasks agree",
        }

    # --- Per-task response assembly ---------------------------------------
    task_payloads: List[Dict[str, Any]] = []
    for r in rta_results:
        task = next(t for t in tasks if t.id == r.task_id)
        slack = (
            task.deadline - r.response_time
            if r.response_time is not None and r.terminal_condition == FIXED_POINT
            else None
        )

        if opts.run_simulation:
            if task.blocking != 0:
                xc = per_task.get(r.task_id, "inconclusive")
            else:
                target = (
                    r.response_time
                    if r.terminal_condition == FIXED_POINT
                    else r.continued_fixed_point
                    if r.terminal_condition == DEADLINE_EXCEEDED
                    else None
                )
                observed = sim_payload["first_job_response_times"].get(r.task_id)
                if target is None:
                    xc = "inconclusive"
                elif observed is None:
                    xc = "inconclusive"
                else:
                    xc = "agree" if observed == target else "disagree"
        else:
            xc = "skipped"

        task_payloads.append(
            {
                "id": task.id,
                "priority": task.priority,
                "wcet": task.wcet,
                "period": task.period,
                "deadline": task.deadline,
                "blocking": task.blocking,
                "schedulable": r.schedulable,
                "response_time": r.response_time,
                "slack": slack,
                "interference_sources": _interference_sources(task, tasks),
                "iterations": r.iterations,
                "terminal_condition": r.terminal_condition,
                "failed_deadline": r.failed_deadline,
                "failure_detail": r.failure_detail,
                "continued_fixed_point": r.continued_fixed_point,
                "rta_crosscheck": xc,
            }
        )

    all_schedulable = all(r.schedulable for r in rta_results)

    total_util = sum(t.wcet / t.period for t in tasks)
    utilization = {
        "total_u": round(total_util, 6),
        "per_task": {t.id: round(t.wcet / t.period, 6) for t in tasks},
        "interpretation": (
            "Utilization is descriptive only. A low/average U is NOT a sufficient "
            "condition for fixed-priority schedulability; use the RTA verdict."
        ),
        "liu_and_layland_rm_bound_n": round(
            len(tasks) * (2 ** (1 / len(tasks)) - 1), 6
        ),
        "note": "The Liu & Layland bound is a sufficient-only RM-specific test and is not used in the verdict.",
    }

    return {
        "schedulable": all_schedulable,
        "model_assumptions": MODEL_ASSUMPTIONS,
        "tasks": task_payloads,
        "simulation": sim_payload,
        "utilization": utilization,
        "integrity": request_digest(req),
    }
