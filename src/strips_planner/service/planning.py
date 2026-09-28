"""Planning orchestration service.

Wires the four engineering boundaries together in one explicit pipeline::

    request dict
      -> language parser        (INPUT_INVALID on shape/syntax errors)
      -> language validator     (INVALID_PROBLEM with issue list)
      -> kernel search          (FOUND / UNSOLVABLE / LIMIT)
      -> independent executor   (replays FOUND plans from init)
      -> evidence store + JSONL log

Every branch ends with a persisted :class:`RunRecord` and a log line carrying
the judgement reason. The service never returns a plan that the independent
executor has not itself replayed from the initial state.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from strips_planner.core.executor import PlanExecutor
from strips_planner.core.search import SearchLimits, SearchStatus, run_search
from strips_planner.errors import (
    ErrorCategory,
    ProblemParseError,
    ProblemValidationError,
    StripsError,
)
from strips_planner.language.parser import parse_dict
from strips_planner.language.validator import validate
from strips_planner.storage.database import EvidenceStore, RunRecord
from strips_planner.storage.run_log import RunLogger

# Server-side safety caps regardless of client-supplied limits.
HARD_MAX_EXPANDED = 1_000_000
HARD_MAX_DEPTH = 10_000
HARD_MAX_SECONDS = 60.0

DEFAULT_MAX_EXPANDED = 100_000
DEFAULT_MAX_DEPTH = 1_000
DEFAULT_MAX_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class PlanOptions:
    algorithm: str = "astar"
    heuristic: str = "hmax"
    max_expanded: int = DEFAULT_MAX_EXPANDED
    max_depth: int = DEFAULT_MAX_DEPTH
    max_seconds: float = DEFAULT_MAX_SECONDS
    capture_events: bool = True
    max_events: int = 500


@dataclass(slots=True)
class ServiceResponse:
    http_status: int
    body: dict[str, Any]


@dataclass(slots=True)
class _Artifact:
    run_id: str
    record: RunRecord


class PlanningService:
    def __init__(self, store: EvidenceStore, logger: RunLogger) -> None:
        self._store = store
        self._log = logger

    # -- raw syntax rejection (run id still assigned for replay) ----------

    def reject_syntax(self, raw_body: bytes, exc: ProblemParseError) -> ServiceResponse:
        """Record a request whose body cannot even be decoded as JSON."""
        run_id = self._new_run_id()
        preview = raw_body.decode("utf-8", errors="replace")[:2_000]
        error = exc.to_dict()
        record = RunRecord(
            run_id=run_id,
            created_at=self._now(),
            category=ErrorCategory.INPUT_INVALID,
            success=False,
            status="REJECTED",
            request={"raw_body_preview": preview},
            error=error,
        )
        self._save(record)
        self._log.event(run_id, "RECEIVED", reason="planning request received (undecodable body)")
        self._log.event(run_id, "FAILED", category=ErrorCategory.INPUT_INVALID,
                        code=exc.code, reason=exc.message)
        return ServiceResponse(400, self._envelope(run_id, None, error))

    # -- planning ----------------------------------------------------------

    def plan(self, payload: Any) -> ServiceResponse:
        run_id = self._new_run_id()
        self._log.event(run_id, "RECEIVED", reason="planning request received")

        try:
            options = self._read_options(payload)
        except ProblemParseError as exc:
            return self._reject(run_id, payload, exc, http_status=400)

        raw_problem_payload = payload.get("problem", payload) if isinstance(payload, dict) else payload
        artifact = _Artifact(run_id=run_id, record=self._base_record(run_id, raw_problem_payload))

        try:
            raw = parse_dict(raw_problem_payload)
            self._log.event(run_id, "PARSED", reason="request shape parsed",
                            action_count=len(raw.actions))
            problem = validate(raw)
            self._log.event(
                run_id, "VALIDATED",
                reason="problem semantically valid; grounding complete",
                ground_actions=len(problem.ground_actions),
                init_atoms=len(problem.init),
                warnings=[w.to_dict() for w in problem.warnings],
            )
        except ProblemParseError as exc:
            return self._reject(run_id, raw_problem_payload, exc, http_status=400)
        except ProblemValidationError as exc:
            return self._reject_problem(run_id, raw_problem_payload, exc)

        problem_summary = self._problem_summary(problem)
        artifact.record.problem_name = problem.name
        artifact.record.problem_summary = problem_summary
        artifact.record.algorithm = options.algorithm
        artifact.record.heuristic = options.heuristic

        try:
            outcome = run_search(
                problem,
                algorithm=options.algorithm,
                heuristic=options.heuristic,
                limits=SearchLimits(
                    max_expanded=options.max_expanded,
                    max_depth=options.max_depth,
                    max_seconds=options.max_seconds,
                ),
                capture_events=options.capture_events,
                max_events=options.max_events,
            )
        except StripsError as exc:
            return self._computation_failed(run_id, artifact, exc)

        search_dict = outcome.to_dict()
        artifact.record.search = search_dict
        self._log.event(
            run_id, "SEARCH", reason=outcome.reason,
            status=outcome.status.value, expanded=outcome.expanded,
            generated=outcome.generated, reopened=outcome.reopened,
            depth=outcome.depth, limit=outcome.limit,
            path_cost=search_dict["path_cost"],
        )

        if outcome.status is SearchStatus.LIMIT:
            return self._limited(run_id, artifact, options)
        if outcome.status is SearchStatus.UNSOLVABLE:
            return self._unsolvable(run_id, artifact)

        return self._verify_and_answer(run_id, artifact, problem, search_dict)

    # -- independent plan replay ------------------------------------------

    def execute_plan(self, payload: Any) -> ServiceResponse:
        run_id = self._new_run_id()
        self._log.event(run_id, "RECEIVED", reason="plan validation request received")
        if not isinstance(payload, dict):
            return self._reject(
                run_id, payload,
                ProblemParseError("request body must be a JSON object",
                                  code="NOT_AN_OBJECT"),
                http_status=400,
            )
        labels = payload.get("plan")
        if not isinstance(labels, list) or not all(isinstance(x, str) for x in labels):
            return self._reject(
                run_id, payload,
                ProblemParseError("'plan' must be a list of action label strings",
                                  code="NOT_A_LIST"),
                http_status=400,
            )
        problem = self._resolve_problem(run_id, payload)
        if isinstance(problem, ServiceResponse):
            return problem

        trace = PlanExecutor(problem).execute(list(labels))
        self._log.event(
            run_id, "EXECUTED",
            reason=trace.message or "all steps executed and goal reached",
            valid=trace.valid, goal_reached=trace.goal_reached,
            failure_code=trace.failure_code, failure_step=trace.failure_step,
            violated_literals=[list(s.violated_literals) for s in trace.steps
                               if s.violated_literals],
        )
        record = self._base_record(run_id, payload)
        record.problem_name = problem.name
        record.problem_summary = self._problem_summary(problem)
        record.validation = {"warnings": [w.to_dict() for w in problem.warnings]}
        record.status = "EXECUTED" if trace.valid else "STATE_CONFLICT"
        body: dict[str, Any] = {
            "run_id": run_id,
            "success": trace.valid,
            "result": {"execution": trace.to_dict()},
            "error": None,
        }
        if trace.valid:
            record.category = "OK"
            record.success = True
            self._save(record)
            self._log.event(run_id, "COMPLETED",
                            reason="plan independently replayed; goal reached",
                            total_cost=trace.total_cost)
            return ServiceResponse(200, body)

        record.category = ErrorCategory.STATE_CONFLICT
        record.success = False
        error = {
            "category": ErrorCategory.STATE_CONFLICT,
            "code": trace.failure_code,
            "message": trace.message,
            "details": {
                "failure_step": trace.failure_step,
                "violated_literals": _first_violation(trace),
            },
        }
        record.error = error
        body["success"] = False
        body["error"] = error
        self._save(record)
        self._log.event(run_id, "FAILED", category=ErrorCategory.STATE_CONFLICT,
                        code=trace.failure_code, reason=trace.message)
        return ServiceResponse(409, body)

    # -- outcome branches --------------------------------------------------

    def _verify_and_answer(self, run_id, artifact, problem, search_dict) -> ServiceResponse:
        labels = list(search_dict["plan"])
        trace = PlanExecutor(problem).execute(labels)
        artifact.record.search = search_dict
        artifact.record.stats = {"execution_steps": len(trace.steps)}
        self._log.event(
            run_id, "EXECUTED",
            reason=trace.message or "independent executor replayed the plan",
            valid=trace.valid, goal_reached=trace.goal_reached,
            failure_code=trace.failure_code, failure_step=trace.failure_step,
        )

        if not trace.valid:
            # Search and the independent executor disagree: never ship the plan.
            error = {
                "category": ErrorCategory.COMPUTATION_FAILED,
                "code": trace.failure_code or "PLAN_NOT_VALIDATED",
                "message": ("planner returned a plan the independent executor "
                            f"rejected: {trace.message}"),
                "details": {
                    "failure_step": trace.failure_step,
                    "violated_literals": _first_violation(trace),
                    "search": {"algorithm": search_dict["algorithm"],
                               "expanded": search_dict["expanded"]},
                },
            }
            artifact.record.category = ErrorCategory.COMPUTATION_FAILED
            artifact.record.success = False
            artifact.record.status = "COMPUTATION_FAILED"
            artifact.record.error = error
            self._save(artifact.record)
            self._log.event(run_id, "FAILED",
                            category=ErrorCategory.COMPUTATION_FAILED,
                            code=error["code"], reason=error["message"])
            return ServiceResponse(500, self._envelope(run_id, None, error))

        if abs(trace.total_cost - search_dict["path_cost"]) > 1e-9:
            error = {
                "category": ErrorCategory.COMPUTATION_FAILED,
                "code": "COST_MISMATCH",
                "message": (f"search cost {search_dict['path_cost']} disagrees with "
                            f"executor cost {trace.total_cost}"),
                "details": {"search_cost": search_dict["path_cost"],
                            "executor_cost": trace.total_cost},
            }
            artifact.record.category = ErrorCategory.COMPUTATION_FAILED
            artifact.record.success = False
            artifact.record.status = "COMPUTATION_FAILED"
            artifact.record.error = error
            self._save(artifact.record)
            return ServiceResponse(500, self._envelope(run_id, None, error))

        result = {
            "status": "FOUND",
            "plan": labels,
            "plan_length": len(labels),
            "path_cost": trace.total_cost,
            "optimal": bool(search_dict["optimal"]),
            "optimality_claim": self._optimality_claim(search_dict),
            "search": {k: v for k, v in search_dict.items() if k != "event_log"},
            "execution": trace.to_dict(),
        }
        artifact.record.category = "OK"
        artifact.record.success = True
        artifact.record.status = "FOUND"
        artifact.record.optimal = bool(search_dict["optimal"])
        artifact.record.path_cost = trace.total_cost
        artifact.record.plan = labels
        self._save(artifact.record)
        self._log.event(
            run_id, "COMPLETED", reason="plan found and independently verified",
            plan_length=len(labels), path_cost=trace.total_cost,
            optimal=search_dict["optimal"],
        )
        return ServiceResponse(200, self._envelope(run_id, result, None))

    def _unsolvable(self, run_id: str, artifact: _Artifact) -> ServiceResponse:
        result = {
            "status": "UNSOLVABLE",
            "plan": [],
            "search": {k: v for k, v in (artifact.record.search or {}).items()
                       if k != "event_log"},
        }
        artifact.record.category = "OK"
        artifact.record.success = True
        artifact.record.status = "UNSOLVABLE"
        self._save(artifact.record)
        self._log.event(run_id, "COMPLETED",
                        reason="reachable space exhausted; goal provably unreachable")
        return ServiceResponse(200, self._envelope(run_id, result, None))

    def _limited(self, run_id: str, artifact: _Artifact, options: PlanOptions) -> ServiceResponse:
        search = artifact.record.search or {}
        error = {
            "category": ErrorCategory.RESOURCE_LIMIT,
            "code": "SEARCH_LIMIT_REACHED",
            "message": search.get("reason", "search stopped at a declared bound"),
            "details": {
                "bound": search.get("limit"),
                "bound_value": search.get("limit_value"),
                "expanded": search.get("expanded"),
                "generated": search.get("generated"),
                "depth_reached": search.get("depth"),
                "solvability": "UNKNOWN",
            },
        }
        result = {"status": "LIMIT", "plan": [], "search":
                  {k: v for k, v in search.items() if k != "event_log"}}
        artifact.record.category = ErrorCategory.RESOURCE_LIMIT
        artifact.record.success = False
        artifact.record.status = "LIMIT"
        artifact.record.error = error
        self._save(artifact.record)
        self._log.event(run_id, "FAILED", category=ErrorCategory.RESOURCE_LIMIT,
                        code="SEARCH_LIMIT_REACHED", reason=error["message"])
        return ServiceResponse(200, self._envelope(run_id, result, error))

    def _reject(self, run_id, payload, exc: StripsError, *, http_status: int) -> ServiceResponse:
        error = exc.to_dict()
        record = self._base_record(run_id, payload)
        record.category = exc.category
        record.success = False
        record.status = "REJECTED"
        record.error = error
        self._save(record)
        self._log.event(run_id, "FAILED", category=exc.category, code=exc.code,
                        reason=exc.message)
        return ServiceResponse(http_status, self._envelope(run_id, None, error))

    def _reject_problem(self, run_id, payload, exc: ProblemValidationError) -> ServiceResponse:
        error = exc.to_dict()
        record = self._base_record(run_id, payload)
        record.category = ErrorCategory.INVALID_PROBLEM
        record.success = False
        record.status = "REJECTED"
        record.validation = error["details"]
        record.error = error
        self._save(record)
        self._log.event(
            run_id, "FAILED", category=ErrorCategory.INVALID_PROBLEM,
            code="INVALID_PROBLEM",
            reason=f"{len(exc.issues)} validation issue(s)",
            issues=[i.to_dict() for i in exc.issues],
        )
        return ServiceResponse(422, self._envelope(run_id, None, error))

    def _computation_failed(self, run_id, artifact: _Artifact, exc: StripsError) -> ServiceResponse:
        error = exc.to_dict()
        artifact.record.category = ErrorCategory.COMPUTATION_FAILED
        artifact.record.success = False
        artifact.record.status = "COMPUTATION_FAILED"
        artifact.record.error = error
        self._save(artifact.record)
        self._log.event(run_id, "FAILED", category=ErrorCategory.COMPUTATION_FAILED,
                        code=exc.code, reason=exc.message)
        return ServiceResponse(500, self._envelope(run_id, None, error))

    # -- helpers -----------------------------------------------------------

    def _resolve_problem(self, run_id, payload):
        if "problem" in payload:
            try:
                raw = parse_dict(payload["problem"])
                return validate(raw)
            except ProblemParseError as exc:
                return self._reject(run_id, payload, exc, http_status=400)
            except ProblemValidationError as exc:
                return self._reject_problem(run_id, payload, exc)
        reference = payload.get("run_id")
        if not isinstance(reference, str):
            return self._reject(
                run_id, payload,
                ProblemParseError("provide either 'problem' or a source 'run_id'",
                                  code="MISSING_FIELD"),
                http_status=400,
            )
        try:
            stored = self._store.get_run(reference)
        except StripsError as exc:
            return self._reject(
                run_id, payload, exc,
                http_status=404 if exc.category == ErrorCategory.NOT_FOUND else 500,
            )
        request = stored.get("request") or {}
        problem_payload = request.get("problem", request)
        try:
            return validate(parse_dict(problem_payload))
        except (ProblemParseError, ProblemValidationError) as exc:
            bad = exc if isinstance(exc, ProblemParseError) else exc
            return self._reject(
                run_id, payload, bad,
                http_status=400 if isinstance(exc, ProblemParseError) else 422,
            )

    def _read_options(self, payload: Any) -> PlanOptions:
        if not isinstance(payload, dict):
            raise ProblemParseError("request body must be a JSON object",
                                    code="NOT_AN_OBJECT")
        raw_options = payload.get("options", {})
        if raw_options is None:
            raw_options = {}
        if not isinstance(raw_options, dict):
            raise ProblemParseError("'options' must be an object", code="NOT_AN_OBJECT")

        algorithm = raw_options.get("algorithm", "astar")
        heuristic = raw_options.get("heuristic", "hmax")
        if not isinstance(algorithm, str) or not isinstance(heuristic, str):
            raise ProblemParseError("algorithm/heuristic must be strings",
                                    code="NOT_A_STRING")
        max_expanded = self._bounded_int(raw_options, "max_expanded",
                                         DEFAULT_MAX_EXPANDED, HARD_MAX_EXPANDED)
        max_depth = self._bounded_int(raw_options, "max_depth",
                                      DEFAULT_MAX_DEPTH, HARD_MAX_DEPTH)
        max_seconds = self._bounded_float(raw_options, "max_seconds",
                                          DEFAULT_MAX_SECONDS, HARD_MAX_SECONDS)
        capture_events = bool(raw_options.get("capture_events", True))
        max_events = self._bounded_int(raw_options, "max_events", 500, 5_000)
        return PlanOptions(
            algorithm=algorithm, heuristic=heuristic,
            max_expanded=max_expanded, max_depth=max_depth,
            max_seconds=max_seconds, capture_events=capture_events,
            max_events=max_events,
        )

    @staticmethod
    def _bounded_int(data, key, default, hard_max) -> int:
        value = data.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ProblemParseError(f"options.{key} must be a positive integer",
                                    code="INVALID_LIMIT")
        return min(value, hard_max)

    @staticmethod
    def _bounded_float(data, key, default, hard_max) -> float:
        value = data.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise ProblemParseError(f"options.{key} must be a positive number",
                                    code="INVALID_LIMIT")
        return min(float(value), hard_max)

    @staticmethod
    def _optimality_claim(search_dict: dict[str, Any]) -> str:
        if not search_dict["optimal"]:
            return "NONE: greedy search gives no cost-optimality guarantee"
        if search_dict["algorithm"] == "bfs":
            return "OPTIMAL: minimum number of actions (costs ignored)"
        if search_dict["algorithm"] == "ucs":
            return "OPTIMAL: minimum summed action cost"
        return f"OPTIMAL: minimum summed action cost (admissible heuristic {search_dict['heuristic']!r})"

    @staticmethod
    def _problem_summary(problem) -> dict[str, Any]:
        return {
            "name": problem.name,
            "type_count": len(problem.types),
            "object_count": sum(len(v) for v in problem.objects.values()),
            "predicate_count": len(problem.predicates),
            "schema_count": len(problem.schemas),
            "ground_action_count": len(problem.ground_actions),
            "init_atom_count": len(problem.init),
            "goal_pos": [str(a) for a in sorted(problem.goal_pos, key=str)],
            "goal_neg": [str(a) for a in sorted(problem.goal_neg, key=str)],
            "warnings": [w.to_dict() for w in problem.warnings],
        }

    def _base_record(self, run_id: str, payload: Any) -> RunRecord:
        return RunRecord(
            run_id=run_id,
            created_at=self._now(),
            category="RECEIVED",
            success=False,
            status="RECEIVED",
            request=payload if _is_jsonable(payload) else {"note": "unserialisable request"},
        )

    def _save(self, record: RunRecord) -> None:
        self._store.save_run(record)

    @staticmethod
    def _envelope(run_id: str, result: dict[str, Any] | None,
                  error: dict[str, Any] | None) -> dict[str, Any]:
        return {"run_id": run_id, "success": error is None,
                "result": result, "error": error}

    @staticmethod
    def _new_run_id() -> str:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        return f"run-{stamp}-{uuid.uuid4().hex[:8]}"

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _first_violation(trace) -> list[str]:
    for step in trace.steps:
        if step.violated_literals:
            return list(step.violated_literals)
    return []


def _is_jsonable(payload: Any) -> bool:
    if isinstance(payload, (dict, list, str, int, float, bool)) or payload is None:
        return True
    return False
