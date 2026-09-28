"""FastAPI service exposing the Krylov expv kernel over HTTP.

Every response and log line carries the request id so a computation can be
traced end to end.  Failures are reported as structured categories, never
as bare stack traces.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import replace

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

import krylov_expv
from krylov_expv.config import ExpvConfig
from krylov_expv.core.integrator import expv
from krylov_expv.errors import ExpvError, InputValidationError, MemoryBudgetExceeded
from krylov_expv.evidence import ExpvEvidence
from krylov_expv.inputs import validate_problem

from .schemas import ExpvRequest, ExpvResponse, FailureOut, StepOut

logger = logging.getLogger("krylov_expv.service")


def _evidence_to_response(
    request_id: str, status: str, w, evidence: ExpvEvidence
) -> ExpvResponse:
    return ExpvResponse(
        request_id=request_id,
        status=status,
        w=None if w is None else [float(x) for x in w],
        total_error_estimate=evidence.total_error_estimate,
        max_subspace_residual=evidence.max_subspace_residual,
        num_steps=evidence.num_steps,
        memory_bytes_used=evidence.memory_bytes_used,
        elapsed_ms=evidence.elapsed_ms,
        versions=evidence.versions,
        steps=[
            StepOut(
                index=s.index,
                t_before=s.t_before,
                tau=s.tau,
                krylov_dim=s.krylov_dim,
                subspace_residual=s.subspace_residual,
                error_estimate=s.error_estimate,
                halvings=s.halvings,
                happy_breakdown=s.happy_breakdown,
            )
            for s in evidence.steps
        ],
        failure=(
            None
            if evidence.failure_category is None
            else FailureOut(
                category=evidence.failure_category,
                message=evidence.failure_message or "",
            )
        ),
    )


def create_app(config: ExpvConfig | None = None) -> FastAPI:
    base_config = config or ExpvConfig.from_env()
    app = FastAPI(title="krylov-expv", version=krylov_expv.__version__)

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["x-request-id"] = request_id
        return response

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/version")
    def version() -> dict[str, object]:
        return {
            "service": "krylov-expv",
            "version": krylov_expv.__version__,
            "versions": ExpvEvidence().versions,
        }

    @app.post("/v1/expv", response_model=ExpvResponse)
    def compute_expv(payload: ExpvRequest, request: Request) -> ExpvResponse | JSONResponse:
        request_id = payload.request_id or request.state.request_id
        overrides = {}
        if payload.m_max is not None:
            overrides["m_max"] = payload.m_max
        if payload.max_steps is not None:
            overrides["max_steps"] = payload.max_steps
        effective = replace(base_config, **overrides)
        logger.info(
            "expv request start",
            extra={"request_id": request_id, "n": payload.matrix.n, "t": payload.t},
        )
        try:
            problem = validate_problem(
                n=payload.matrix.n,
                row=payload.matrix.row,
                col=payload.matrix.col,
                data=payload.matrix.data,
                vector=payload.vector,
                t=payload.t,
                tol=payload.tol,
                default_tol=base_config.tol,
            )
        except InputValidationError as exc:
            logger.warning(
                "expv rejected: %s", exc.message,
                extra={"request_id": request_id, "category": exc.category},
            )
            return JSONResponse(
                status_code=400,
                content={
                    "request_id": request_id,
                    "status": "rejected",
                    "failure": {"category": exc.category, "message": exc.message},
                },
            )

        try:
            result = expv(
                problem.matrix, problem.t, problem.vector,
                config=effective, tol=problem.tol,
            )
        except MemoryBudgetExceeded as exc:
            evidence = ExpvEvidence()
            evidence.record_failure(exc.category, exc.message)
            logger.warning(
                "expv rejected: %s", exc.message,
                extra={"request_id": request_id, "category": exc.category},
            )
            return JSONResponse(
                status_code=413,
                content=_evidence_to_response(request_id, "rejected", None, evidence).model_dump(),
            )
        except ExpvError as exc:  # pragma: no cover - defensive catch-all
            evidence = ExpvEvidence()
            evidence.record_failure(exc.category, exc.message)
            return JSONResponse(
                status_code=500,
                content=_evidence_to_response(request_id, "rejected", None, evidence).model_dump(),
            )

        status = "converged" if result.converged else "not_converged"
        logger.info(
            "expv request done: status=%s steps=%d error_estimate=%.3e elapsed_ms=%.2f",
            status,
            result.evidence.num_steps,
            result.evidence.total_error_estimate,
            result.evidence.elapsed_ms,
            extra={"request_id": request_id},
        )
        return _evidence_to_response(request_id, status, result.w, result.evidence)

    return app


app = create_app()
