"""End-to-end planning pipeline and its contracts.

Request envelope::

    {
      "domain": {...}, "problem": {...},
      "options": {
        "algorithm": "bfs|ucs|astar",
        "heuristic": "zero|h_max|h_add",
        "max_expansions": 100000,
        "max_frontier": 100000,
        "max_depth": null,
        "time_limit_seconds": 30.0
      }
    }

Response envelope on success::

    {
      "run_id": "...",
      "verdict": "solved|unsolvable|unknown",
      "reason": null,
      "optimal_guarantee": true,
      "plan": [...], "cost": 7, "path_length": 5,
      "verification": {"valid": true, "goal_reached": true, ...},
      "search": {...counters and trace...}
    }

Failure envelope (HTTP 4xx/5xx)::

    {"run_id": "...", "error": {"category": "...", "code": "...",
                                "message": "...", "details": [...]}}
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

from .encoding import state_hash
from .errors import (
    COMPUTATION_FAILED,
    INTERNAL_VERIFICATION_FAILED,
    INPUT_ERROR,
    RESOURCE_EXHAUSTED,
    REQUEST_MALFORMED,
    STATE_CONFLICT,
    ComputationError,
    PlannerError,
    ResourceLimitError,
    ValidationError,
)
from .evidence import EvidenceStore
from .executor import execute_plan
from .grounding import DEFAULT_GROUND_ACTIONS_LIMIT, ground
from .model import state_to_texts
from .parser import parse_domain, parse_problem
from .search import (
    ASTAR,
    BFS,
    UCS,
    SearchConfig,
    search,
)
from .validation import validate

logger = logging.getLogger("strips_planner")

ALGORITHMS = {BFS, UCS, ASTAR}
HEURISTIC_NAMES = {"zero", "h_max", "h_add"}


@dataclass(frozen=True)
class PipelineOptions:
    algorithm: str = ASTAR
    heuristic: str = "h_max"
    max_expansions: int = 100_000
    max_frontier: int = 100_000
    max_depth: Optional[int] = None
    time_limit_seconds: float = 30.0
    ground_actions_limit: int = DEFAULT_GROUND_ACTIONS_LIMIT
    include_trace: bool = True

    def to_search_config(self) -> SearchConfig:
        heuristic = "zero" if self.algorithm == BFS else self.heuristic
        return SearchConfig(
            algorithm=self.algorithm,
            heuristic=heuristic,
            max_expansions=self.max_expansions,
            max_frontier=self.max_frontier,
            max_depth=self.max_depth,
            time_limit_seconds=self.time_limit_seconds,
        )


def options_from_dict(raw: Any) -> PipelineOptions:
    if raw is None:
        return PipelineOptions()
    if not isinstance(raw, dict):
        raise ValidationError("options must be an object", code=REQUEST_MALFORMED)

    algorithm = raw.get("algorithm", ASTAR)
    if algorithm not in ALGORITHMS:
        raise ValidationError(
            f"algorithm must be one of {sorted(ALGORITHMS)}",
            code=REQUEST_MALFORMED,
        )
    heuristic = raw.get("heuristic", "h_max")
    if heuristic not in HEURISTIC_NAMES:
        raise ValidationError(
            f"heuristic must be one of {sorted(HEURISTIC_NAMES)}",
            code=REQUEST_MALFORMED,
        )

    def positive_int(name: str, default: int) -> int:
        value = raw.get(name, default)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ValidationError(
                f"options.{name} must be a positive integer",
                code=REQUEST_MALFORMED,
            )
        return value

    max_depth = raw.get("max_depth", None)
    if max_depth is not None and (
        not isinstance(max_depth, int)
        or isinstance(max_depth, bool)
        or max_depth < 0
    ):
        raise ValidationError(
            "options.max_depth must be a non-negative integer or null",
            code=REQUEST_MALFORMED,
        )

    time_limit = raw.get("time_limit_seconds", 30.0)
    if not isinstance(time_limit, (int, float)) or isinstance(time_limit, bool) \
            or not 0 < time_limit <= 600:
        raise ValidationError(
            "options.time_limit_seconds must be a number in (0, 600]",
            code=REQUEST_MALFORMED,
        )

    return PipelineOptions(
        algorithm=algorithm,
        heuristic=heuristic,
        max_expansions=positive_int("max_expansions", 100_000),
        max_frontier=positive_int("max_frontier", 100_000),
        max_depth=max_depth,
        time_limit_seconds=float(time_limit),
        ground_actions_limit=positive_int(
            "ground_actions_limit", DEFAULT_GROUND_ACTIONS_LIMIT
        ),
        include_trace=bool(raw.get("include_trace", True)),
    )


def run_pipeline(
    request: dict[str, Any],
    store: EvidenceStore,
    *,
    run_id: Optional[str] = None,
) -> dict[str, Any]:
    """Execute one planning request and persist its evidence.

    Raises subclasses of :class:`PlannerError` for the four failure
    categories; the HTTP layer maps those to envelopes. Evidence for the
    run is written in both success and failure paths.
    """
    run_id = run_id or store.new_run_id()
    started = time.monotonic()
    log = logger

    try:
        options = options_from_dict(request.get("options"))
        domain = parse_domain(request.get("domain"))
        problem = parse_problem(request.get("problem"), domain)
        validate(domain, problem)
        gp = ground(
            domain, problem, limit=options.ground_actions_limit
        )
        log.info(
            "run %s grounded: domain=%s problem=%s actions=%d initial=%s",
            run_id, domain.name, problem.name, len(gp.actions),
            state_to_texts(gp.initial),
        )

        result = search(gp, options.to_search_config())
        search_dict = result.to_dict()
        log.info(
            "run %s search verdict=%s expanded=%d generated=%d cost=%s",
            run_id, result.status, result.expanded, result.generated,
            result.cost,
        )

        verification: Optional[dict[str, Any]] = None
        plan_payload: list[dict[str, Any]] = search_dict["plan"]

        if result.status == "solved":
            report = execute_plan(gp, plan_payload)
            verification = report.to_dict()
            if not report.valid:
                # The kernel produced a plan its own executor rejects:
                # a service defect, never a caller problem.
                raise ComputationError(
                    "search returned a plan that failed independent "
                    "verification",
                    code=INTERNAL_VERIFICATION_FAILED,
                    details=[{"verification": verification}],
                )
            store.insert_steps(run_id, verification["steps"])
            log.info(
                "run %s verified plan: %d steps, cost %d",
                run_id, report.total_cost, len(report.steps),
            )

        if options.include_trace:
            store.insert_trace(run_id, search_dict["trace"])

        elapsed = time.monotonic() - started
        store.insert_run(
            run_id=run_id,
            request=request,
            domain_name=domain.name,
            problem_name=problem.name,
            algorithm=options.algorithm,
            heuristic=options.heuristic if options.algorithm != BFS else None,
            status="ok",
            search_dict=search_dict,
            plan=plan_payload if result.status == "solved" else None,
            verification=verification,
            elapsed_seconds=elapsed,
        )

        return {
            "run_id": run_id,
            "verdict": result.status,
            "reason": result.reason,
            "optimal_guarantee": result.optimal_guarantee,
            "plan": plan_payload if result.status == "solved" else [],
            "cost": result.cost,
            "path_length": result.path_length,
            "verification": verification,
            "search": {
                k: v for k, v in search_dict.items()
                if k not in ("plan", "trace")
            },
            "trace": search_dict["trace"] if options.include_trace else [],
            "states": {
                "initial_hash": state_hash(gp.initial),
                "initial": state_to_texts(gp.initial),
            },
        }

    except PlannerError as exc:
        _record_failure(store, run_id, request, exc, started)
        raise
    except Exception as exc:  # noqa: BLE001 - convert to contract error
        wrapped = ComputationError(f"unexpected failure: {exc}")
        _record_failure(store, run_id, request, wrapped, started)
        raise wrapped from exc


def _record_failure(
    store: EvidenceStore,
    run_id: str,
    request: dict[str, Any],
    exc: PlannerError,
    started: float,
) -> None:
    elapsed = time.monotonic() - started
    category = exc.category
    domain_name = None
    problem_name = None
    try:
        domain = request.get("domain")
        problem = request.get("problem")
        if isinstance(domain, dict):
            domain_name = domain.get("name")
        if isinstance(problem, dict):
            problem_name = problem.get("name")
    except AttributeError:
        pass

    store.insert_run(
        run_id=run_id,
        request=_safe_request(request),
        domain_name=domain_name,
        problem_name=problem_name,
        algorithm=None,
        heuristic=None,
        status=category,
        elapsed_seconds=elapsed,
    )
    store.insert_error(
        run_id=run_id,
        category=category,
        code=exc.code,
        message=str(exc),
        details=exc.details,
    )
    logger.warning(
        "run %s failed category=%s code=%s: %s",
        run_id, category, exc.code, exc,
    )


def _safe_request(request: Any) -> dict[str, Any]:
    if isinstance(request, dict):
        return request
    return {"_raw": str(request)}
