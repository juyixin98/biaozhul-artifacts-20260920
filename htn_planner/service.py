"""Orchestration: parse -> plan -> independently verify -> persist."""

from __future__ import annotations

import dataclasses
import logging
from typing import Any

from . import __version__
from .core.engine import Bounds, Planner, PlanningError
from .lang import LangError, parse_domain, parse_problem
from .store import EvidenceStore
from .verify import Verifier

logger = logging.getLogger("htn_planner.service")


class RequestError(ValueError):
    """Client-facing request error with an HTTP status hint."""

    def __init__(self, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


@dataclasses.dataclass(frozen=True)
class PlanResponse:
    result: dict[str, Any]
    verification: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"result": self.result, "verification": self.verification}


class PlanningService:
    def __init__(
        self,
        store: EvidenceStore,
        default_bounds: Bounds,
        verifier: Verifier | None = None,
    ) -> None:
        self.store = store
        self.default_bounds = default_bounds
        self.verifier = verifier or Verifier()

    def plan(
        self,
        domain_text: str,
        problem_text: str,
        request_id: str,
        domain_version: str = "local-unversioned",
        bounds_overrides: dict[str, int] | None = None,
    ) -> PlanResponse:
        logger.info("parsing domain and problem")
        try:
            domain = parse_domain(domain_text)
            problem = parse_problem(problem_text)
        except LangError as exc:
            logger.warning("parse error: %s", exc)
            raise RequestError(f"rule language error: {exc}", status_code=422) from exc

        bounds = self._merge_bounds(bounds_overrides)
        planner = Planner(bounds)
        logger.info(
            "planning domain=%s problem=%s engine=%s",
            domain.name,
            problem.name,
            __version__,
        )
        try:
            result = planner.solve(domain, problem, request_id, domain_version)
        except PlanningError as exc:
            logger.warning("planning error: %s", exc)
            raise RequestError(str(exc), status_code=422) from exc

        logger.info(
            "status=%s actions=%d dead_ends=%d expansions=%d backtracks=%d",
            result.status,
            len(result.plan),
            len(result.failures),
            result.counters.get("expansions", 0),
            result.counters.get("backtracks", 0),
        )

        report = self.verifier.verify(domain, problem, result)
        logger.info(
            "verification ok=%s executable=%s hierarchy=%s order=%s",
            report.ok,
            report.executable,
            report.hierarchy_consistent,
            report.order_consistent,
        )

        self.store.save_run(request_id, domain_text, problem_text, result, domain_version)
        self.store.save_verification(request_id, report)
        logger.info("evidence persisted")

        return PlanResponse(result=result.to_dict(), verification=report.to_dict())

    def _merge_bounds(self, overrides: dict[str, int] | None) -> Bounds:
        if not overrides:
            return self.default_bounds
        valid = {f.name for f in dataclasses.fields(Bounds)}
        unknown = sorted(set(overrides) - valid)
        if unknown:
            raise RequestError(
                f"unknown bound key(s): {unknown}; valid: {sorted(valid)}",
                status_code=422,
            )
        merged = {**dataclasses.asdict(self.default_bounds), **overrides}
        for key, value in merged.items():
            if not isinstance(value, int) or value < 1:
                raise RequestError(f"bound {key} must be a positive integer")
        return Bounds(**merged)
