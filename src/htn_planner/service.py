"""Application service: orchestrates registry, kernel, audit, and evidence store.

This is the only layer that touches storage and HTTP identity; the kernel and
rule language stay pure and independently testable.
"""
from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .config import SETTINGS, Settings
from .models import Domain, PlanResult, Problem
from .planner import Planner
from .rule_language import RuleLanguageError, load_domain, load_problem
from .storage import EvidenceStore

logger = logging.getLogger("htn_planner.service")


class ServiceError(Exception):
    """Domain/problem input error (4xx class)."""


class Registry:
    """In-memory cache of domains loaded from the local fixture directory."""

    def __init__(self, domain_dir: str) -> None:
        self._domain_dir = Path(domain_dir)
        self._lock = threading.Lock()
        self._cache: dict[str, Domain] = {}

    def names(self) -> list[str]:
        loaded = {p.stem for p in self._domain_dir.glob("*.yaml")}
        loaded |= {p.stem for p in self._domain_dir.glob("*.yml")}
        return sorted(loaded)

    def load(self, name: str) -> Domain:
        with self._lock:
            if name in self._cache:
                return self._cache[name]
            matches = list(self._domain_dir.glob(f"{name}.y*ml"))
            if not matches:
                raise ServiceError(
                    f"unknown domain {name!r}; available={self.names()}"
                )
            domain = load_domain(matches[0])
            self._cache[name] = domain
            return domain

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()


def _new_request_id() -> str:
    return f"req-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"


class PlanningService:
    def __init__(
        self,
        settings: Settings | None = None,
        store: EvidenceStore | None = None,
        registry: Registry | None = None,
    ) -> None:
        self.settings = settings or SETTINGS
        self.registry = registry or Registry(self.settings.domain_dir)
        self.store = store or EvidenceStore(self.settings.db_path)

    # -- fixture-driven runs ---------------------------------------------
    def plan_from_fixture(
        self, problem_name: str, request_id: str | None = None
    ) -> PlanResult:
        request_id = request_id or _new_request_id()
        problem_path = self._resolve_problem(problem_name)
        try:
            problem = load_problem(problem_path)
            domain = self.registry.load(problem.domain)
        except RuleLanguageError as exc:
            raise ServiceError(str(exc)) from exc
        return self._run(problem, domain, request_id, source=str(problem_path))

    # -- ad-hoc runs ------------------------------------------------------
    def plan_inline(
        self, problem: Problem, request_id: str | None = None
    ) -> PlanResult:
        request_id = request_id or _new_request_id()
        try:
            domain = self.registry.load(problem.domain)
        except RuleLanguageError as exc:
            raise ServiceError(str(exc)) from exc
        return self._run(problem, domain, request_id, source="inline-request")

    def _resolve_problem(self, name: str) -> Path:
        # Allow both a bare fixture name and an explicit relative filename.
        candidates = [
            Path(self.settings.fixture_dir) / "problems" / name,
            Path(self.settings.fixture_dir) / "problems" / f"{name}.yaml",
        ]
        for c in candidates:
            if c.exists():
                return c
        raise ServiceError(f"unknown problem fixture {name!r}")

    def _run(
        self, problem: Problem, domain: Domain, request_id: str, source: str
    ) -> PlanResult:
        audit: list[dict] = []

        def sink(location: str, message: str) -> None:
            audit.append(
                {
                    "location": location,
                    "message": message,
                    "at": datetime.now(timezone.utc).isoformat(),
                }
            )

        audit.append(
            {
                "location": "service:receive",
                "message": (
                    f"received request_id={request_id} source={source} "
                    f"problem={problem.name} service={self.settings.service_name}"
                    f"@{self.settings.service_version}"
                ),
                "at": datetime.now(timezone.utc).isoformat(),
            }
        )
        logger.info(
            "plan.start request_id=%s problem=%s source=%s",
            request_id, problem.name, source,
        )
        planner = Planner(domain, audit_sink=sink)
        result = planner.plan(problem, request_id=request_id)

        # Uncertain conclusions are explicitly separated, never marked passed.
        if result.feasible and result.abandoned_branches:
            result.uncertainty.append(
                "plan feasible, but "
                f"{len(result.abandoned_branches)} alternative branch(es) were "
                "explored and rejected during backtracking (see abandoned_branches)"
            )

        verdict = "FEASIBLE" if result.feasible else "INFEASIBLE"
        terminal = result.failures[-1].kind.value if result.failures else "none"
        audit.append(
            {
                "location": "service:respond",
                "message": (
                    f"{verdict} request_id={request_id} "
                    f"terminal_failure={terminal} leaves={len(result.execution_order)} "
                    f"depth={result.depth_used} expansions={result.expansions_used}"
                ),
                "at": datetime.now(timezone.utc).isoformat(),
            }
        )
        self.store.save(result, audit)
        logger.info(
            "plan.end request_id=%s feasible=%s terminal=%s leaves=%d depth=%d expansions=%d",
            request_id, result.feasible, terminal,
            len(result.execution_order), result.depth_used, result.expansions_used,
        )
        return result
