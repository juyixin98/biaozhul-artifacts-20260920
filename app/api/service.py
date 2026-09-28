"""Solve orchestration: model -> solver -> evidence store."""

from __future__ import annotations

import logging
import uuid

from app import __version__
from app.solver.models import CSPModel
from app.solver.search import Solver
from app.store.evidence import EvidenceStore

logger = logging.getLogger("csp.service")


class SolveService:
    def __init__(self, store: EvidenceStore):
        self.store = store

    def solve(
        self,
        model: CSPModel,
        max_nodes: int,
        max_backtracks: int,
        collect_reasons: bool,
    ) -> tuple[str, dict]:
        run_id = uuid.uuid4().hex
        logger.info(
            "solve start run_id=%s model=%s variables=%d constraints=%d",
            run_id,
            model.name,
            model.variable_count(),
            model.constraint_count(),
        )
        result = Solver(model).solve(
            max_nodes=max_nodes,
            max_backtracks=max_backtracks,
            collect_reasons=collect_reasons,
        )
        payload = model.model_dump(mode="json")
        self.store.save_run(
            run_id=run_id,
            model=payload,
            status=result.status.value,
            solution=result.solution,
            stats=result.stats,
            failure=result.failure,
            reasons=result.reason_trace,
            solver_version=__version__,
        )
        logger.info(
            "solve done run_id=%s status=%s nodes=%d backtracks=%d prunes=%d",
            run_id,
            result.status.value,
            result.stats["nodes"],
            result.stats["backtracks"],
            result.stats["prunes"],
        )
        response = {
            "run_id": run_id,
            "status": result.status.value,
            "solution": result.solution,
            "domains": result.domains,
            "stats": result.stats,
            "failure": result.failure,
            "reasons": result.reason_trace,
            "solver_version": __version__,
        }
        return run_id, response
