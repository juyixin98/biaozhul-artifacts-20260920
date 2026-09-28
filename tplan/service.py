"""Service orchestration: parse -> plan -> independent replay -> evidence.

This is the only layer that touches the store; the HTTP layer stays thin and
the kernel stays pure. Every verdict carries:

* ``run_id`` and the input SHA-256 (so logs correlate with the exact input),
* solver + replay status and an explicit failure category,
* budget usage and the search trace (decision steps and pruning reasons).
"""

from __future__ import annotations

import hashlib
import json
import logging
import platform
import time
from dataclasses import dataclass
from typing import Any

from . import __version__
from .config import Settings
from .model import FailureCode, Problem, ScheduledAction
from .simulator import simulate
from .solver import Budget, Solver
from .store import EvidenceStore

logger = logging.getLogger("tplan")


def input_fingerprint(raw: Any) -> str:
    payload = json.dumps(raw, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


@dataclass
class RunResponse:
    run_id: str
    status: str
    failure_code: str | None
    optimal: bool
    schedule: list[dict[str, int | str]]
    goal_time: int | None
    best_cost: int | None
    nodes_expanded: int
    node_budget: int
    time_budget_seconds: float
    elapsed_seconds: float
    message: str
    timeline: list[dict[str, Any]]
    trace: list[dict[str, Any]]
    input_sha256: str
    engine: dict[str, str]


class PlanningService:
    def __init__(self, settings: Settings, store: EvidenceStore | None = None):
        self.settings = settings
        self.store = store or EvidenceStore(settings.db_path)

    def close(self) -> None:
        self.store.close()

    def solve(
        self,
        raw: dict[str, Any],
        node_budget: int | None = None,
        time_budget_seconds: float | None = None,
        run_id: str | None = None,
    ) -> RunResponse:
        run_id = run_id or EvidenceStore.new_run_id()
        fingerprint = input_fingerprint(raw)
        node_budget = node_budget or self.settings.default_node_budget
        time_budget_s = time_budget_seconds or self.settings.default_time_budget_seconds
        started = time.monotonic()
        engine = {
            "service_version": __version__,
            "python": platform.python_version(),
            "platform": platform.platform(),
        }

        try:
            problem = Problem.from_dict(raw)
            if problem.horizon > self.settings.max_horizon:
                raise ValueError(
                    f"horizon {problem.horizon} exceeds limit {self.settings.max_horizon}"
                )
            if len(problem.actions) > self.settings.max_actions:
                raise ValueError(
                    f"{len(problem.actions)} actions exceed the limit "
                    f"{self.settings.max_actions}"
                )
        except (ValueError, TypeError) as exc:
            elapsed = time.monotonic() - started
            logger.warning(
                "run_id=%s input=%s rejected invalid_problem: %s",
                run_id, fingerprint, exc,
            )
            self.store.save_invalid(
                run_id, raw, str(exc), node_budget, time_budget_s,
                horizon=raw.get("horizon") if isinstance(raw, dict) else None,
            )
            return RunResponse(
                run_id=run_id, status="invalid",
                failure_code=FailureCode.INVALID_PROBLEM.value,
                optimal=False, schedule=[], goal_time=None, best_cost=None,
                nodes_expanded=0, node_budget=node_budget,
                time_budget_seconds=time_budget_s, elapsed_seconds=round(elapsed, 6),
                message=f"invalid problem: {exc}", timeline=[], trace=[],
                input_sha256=fingerprint, engine=engine,
            )

        if len(problem.actions) > self.settings.max_actions:
            raise ValueError  # guarded at API layer; defensive only

        deadline = started + time_budget_s
        solver = Solver(problem, Budget(node_limit=node_budget, deadline_monotonic=deadline),
                        max_repeats=self.settings.max_repeats)
        result = solver.solve()
        elapsed = time.monotonic() - started

        # Independent replay of any accepted plan (the solver already replays
        # internally; the service re-replays to populate the evidence timeline).
        replay = simulate(problem, list(result.schedule)) if result.found else None
        if replay is not None and not replay.ok:
            # Defense in depth: never surface an unverifiable plan as success.
            err = replay.error
            logger.error(
                "run_id=%s input=%s REPLAY_MISMATCH at t=%s: %s",
                run_id, fingerprint, err.time if err else None,
                err.message if err else "unknown",
            )
        else:
            logger.info(
                "run_id=%s input=%s status=%s failure=%s cost=%s goal_time=%s nodes=%s/%s "
                "elapsed=%.4fs replay_ok=%s",
                run_id, fingerprint, result.status,
                result.failure_code.value if result.failure_code else None,
                result.best_cost, result.goal_time, result.nodes_expanded, node_budget,
                elapsed, replay.ok if replay else "n/a",
            )

        self.store.save_run(
            run_id, raw, problem, result, replay, node_budget, time_budget_s
        )

        timeline = [
            {
                "time": ev.time,
                "order": ev.order,
                "kind": ev.kind.value,
                "action_id": ev.action_id,
                "state": ev.state,
                "resources": ev.resources,
                "note": ev.note,
            }
            for ev in (replay.events if replay else [])
        ]
        return RunResponse(
            run_id=run_id,
            status=result.status,
            failure_code=result.failure_code.value if result.failure_code else None,
            optimal=result.status == "optimal",
            schedule=[
                {"action_id": s.action_id, "start": s.start, "end": s.end}
                for s in result.schedule
            ],
            goal_time=result.goal_time,
            best_cost=result.best_cost,
            nodes_expanded=result.nodes_expanded,
            node_budget=node_budget,
            time_budget_seconds=time_budget_s,
            elapsed_seconds=round(elapsed, 6),
            message=result.message,
            timeline=timeline,
            trace=list(result.trace),
            input_sha256=fingerprint,
            engine=engine,
        )

    def replay(self, raw: dict[str, Any], schedule: list[dict[str, Any]]) -> dict[str, Any]:
        """Independently replay a caller-supplied schedule (no search).

        Reports the exact failure category (never a generic error) plus the
        full timeline up to the rejected point.
        """
        problem = self.validate_only(raw)
        items = [
            ScheduledAction(str(s["action_id"]), int(s["start"]), int(s["end"]))
            for s in schedule
        ]
        outcome = simulate(problem, items)
        return {
            "ok": outcome.ok,
            "goal_met": outcome.goal_met,
            "failure_code": outcome.error.code.value if outcome.error else None,
            "failure_time": outcome.error.time if outcome.error else None,
            "message": outcome.error.message if outcome.error else "schedule is valid",
            "timeline": [
                {
                    "time": ev.time,
                    "order": ev.order,
                    "kind": ev.kind.value,
                    "action_id": ev.action_id,
                    "state": ev.state,
                    "resources": ev.resources,
                    "note": ev.note,
                }
                for ev in outcome.events
            ],
        }

    def validate_only(self, raw: dict[str, Any]) -> Problem:
        """Parse a problem without solving (raises ValueError on bad input)."""
        if not isinstance(raw, dict):
            raise ValueError("request body must be a JSON object")
        if not isinstance(raw.get("horizon"), int) or not (0 <= raw["horizon"] <= self.settings.max_horizon):
            raise ValueError(f"horizon must be an integer in [0,{self.settings.max_horizon}]")
        problem = Problem.from_dict(raw)
        if len(problem.actions) > self.settings.max_actions:
            raise ValueError(f"at most {self.settings.max_actions} actions are allowed")
        return problem
