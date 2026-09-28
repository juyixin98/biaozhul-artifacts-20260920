"""FastAPI application factory and HTTP routes (thin)."""

from __future__ import annotations

from typing import Any, Dict, List

import numpy as np
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from .core.errors import (
    ActivationRecomputeError,
    ErrorCategory,
)
from .core.numeric import fingerprint
from .fixtures.specs import ALL_SPECS
from .api.schemas import (
    ApplyRequest,
    CreateSessionRequest,
    GradientSummary,
    PlanRequest,
    PlanResponse,
    RunRequest,
    RunResponse,
    SessionResponse,
    VerificationReport,
)
from .service.orchestrator import Orchestrator
from .service.runlog import RunLogger
from .service.sessions import SessionStore

_HTTP_STATUS = {
    ErrorCategory.INPUT_ERROR: 400,
    ErrorCategory.STATE_CONFLICT: 409,
    ErrorCategory.RESOURCE_EXHAUSTED: 507,
    ErrorCategory.COMPUTATION_FAILURE: 500,
}


def create_app(orchestrator: Orchestrator | None = None) -> FastAPI:
    app = FastAPI(
        title="Activation Recompute Scheduler",
        version="1.0.0",
        description=(
            "Checkpoint planning and reverse-mode recomputation for small "
            "synthetic training graphs. All data is local and deterministic."
        ),
    )
    orch = orchestrator or Orchestrator(SessionStore(), RunLogger())
    app.state.orchestrator = orch

    @app.exception_handler(ActivationRecomputeError)
    async def _handle_classified(_request, exc: ActivationRecomputeError):
        status = _HTTP_STATUS.get(exc.category, 500)
        return JSONResponse(status_code=status, content={"error": exc.to_dict()})

    @app.get("/health")
    async def health() -> Dict[str, str]:
        return {"status": "ok"}

    @app.get("/fixtures")
    async def fixtures() -> Dict[str, Any]:
        return {
            "fixtures": [
                {"name": name, "description": spec["description"],
                 "target": spec["target"], "nodes": len(spec["nodes"])}
                for name, spec in ALL_SPECS.items()
            ]
        }

    @app.post("/sessions", response_model=SessionResponse, status_code=201)
    async def create_session(req: CreateSessionRequest) -> SessionResponse:
        session = orch.create_session(req)
        return _session_response(session)

    @app.get("/sessions/{session_id}", response_model=SessionResponse)
    async def get_session(session_id: str) -> SessionResponse:
        return _session_response(orch.store.get(session_id))

    @app.post("/sessions/{session_id}/plan", response_model=PlanResponse)
    async def plan(session_id: str, req: PlanRequest) -> PlanResponse:
        session = orch.store.get(session_id)
        result = orch.plan(session, req)
        feasible = (
            req.memory_budget is None
            or result["plan"]["peak_memory"] <= req.memory_budget
        )
        if req.memory_budget is None:
            note = "no budget supplied; plan minimises recompute unconstrained"
        elif feasible:
            note = "plan fits the requested memory budget"
        else:
            note = "plan does not fit the requested budget"
        return PlanResponse(
            session_id=session_id, run_id=result["run_id"],
            plan={**result["plan"],
                  "independent_check": result["independent_check"]},
            feasible=feasible, note=note,
        )

    @app.post("/sessions/{session_id}/runs", response_model=RunResponse)
    async def run(session_id: str, req: RunRequest) -> RunResponse:
        session = orch.store.get(session_id)
        result = orch.run(session, req)
        return _run_response(session_id, result, session.training.step)

    @app.post("/sessions/{session_id}/apply")
    async def apply(session_id: str, req: ApplyRequest) -> Dict[str, Any]:
        session = orch.store.get(session_id)
        result = orch.apply(session, req.lr)
        return {
            "session_id": session_id,
            "run_id": result["run_id"],
            "training_step": result["training_step"],
        }

    return app


def _session_response(session) -> SessionResponse:
    g = session.graph
    return SessionResponse(
        session_id=session.session_id,
        fixture=session.fixture_name,
        target=g.target,
        order=list(g.order),
        shapes={nid: list(shape) for nid, shape in g.shapes.items()},
        shared_nodes=sorted(nid for nid in g.user_count if g.is_shared(nid)),
        training_step=session.training.step,
        planned=session.planned,
        run_count=len(session.runs),
    )


def _run_response(session_id: str, result: Dict[str, Any],
                  training_step: int) -> RunResponse:
    run = result["run"]
    log = result["replay_log"]
    verification = result["verification"]
    grad_summaries: List[GradientSummary] = [
        GradientSummary(
            parameter=pid,
            shape=list(np.asarray(g).shape),
            fingerprint=fingerprint(np.asarray(g)),
            max_abs=float(np.max(np.abs(np.asarray(g)))),
            l2_norm=float(np.linalg.norm(np.asarray(g))),
        )
        for pid, g in sorted(run.grads.items())
    ]
    report = VerificationReport(
        oracle_source=verification["oracle_source"],
        loss_abs_diff=verification["loss_abs_diff"],
        grad_max_abs_diff=verification["grad_max_abs_diff"],
        finite_difference_max_abs_diff=(
            verification["finite_difference_max_abs_diff"]
        ),
        passed=verification["passed"],
        reasons=verification["reasons"],
        tolerances=verification["tolerances"],
    )
    return RunResponse(
        session_id=session_id,
        run_id=run.run_id,
        status=run.status.value,
        loss=run.loss,
        peak_memory=run.peak_memory,
        forward_flops=run.forward_flops,
        recompute_flops=run.recompute_flops,
        backward_flops=run.backward_flops,
        extra_compute_ratio=(
            run.recompute_flops / run.forward_flops if run.forward_flops else 0.0
        ),
        recomputed_nodes=list(run.recomputed_nodes),
        replay_waves=log["replay_waves"],
        emitted_side_effects=run.emitted_side_effects,
        rng_strategy=log["rng_strategy"],
        rng_replay_ok=run.rng_replay_ok,
        gradients=grad_summaries,
        verification=report,
        training_step=training_step,
    )


app = create_app()
