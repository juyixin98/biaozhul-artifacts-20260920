"""FastAPI application: HTTP boundary for the HVP service."""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .config import ServiceConfig, load_config
from .errors import ErrorCategory, ServiceError
from .logging_config import log_event
from .schemas import (
    CreateGraphRequest,
    CreateGraphResponse,
    GraphInfoResponse,
    HvpRequest,
    HvpResponse,
    SetPointRequest,
    SetPointResponse,
)
from .service import HvpRequestData, HvpService


def create_app(config: ServiceConfig | None = None, service: HvpService | None = None) -> FastAPI:
    svc = service or HvpService(config or load_config())
    app = FastAPI(
        title="HVP Service",
        version="0.1.0",
        description="Hessian-vector products for restricted differentiable "
        "expression graphs, without materializing the Hessian.",
    )
    app.state.service = svc

    @app.exception_handler(ServiceError)
    async def service_error_handler(request: Request, exc: ServiceError) -> JSONResponse:
        log_event(
            logging.INFO,
            "request_failed",
            run_id=exc.run_id,
            category=exc.category.value,
            reason=exc.message,
            details=exc.details,
            path=request.url.path,
        )
        return JSONResponse(status_code=exc.http_status, content=exc.to_payload())

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        payload = {
            "error": {
                "category": ErrorCategory.INPUT_VALIDATION.value,
                "message": "request body failed schema validation",
                "details": {"errors": exc.errors()},
                "run_id": None,
            }
        }
        return JSONResponse(status_code=400, content=payload)

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok", "graphs": len(svc.store)}

    @app.post("/graphs", response_model=CreateGraphResponse, status_code=201)
    async def create_graph(req: CreateGraphRequest) -> CreateGraphResponse:
        graph_id, graph = svc.create_graph(
            [n.model_dump() for n in req.nodes], req.output
        )
        return CreateGraphResponse(
            graph_id=graph_id,
            node_count=graph.node_count,
            output=graph.output,
            layout=graph.layout.to_dict(),
        )

    @app.get("/graphs/{graph_id}", response_model=GraphInfoResponse)
    async def get_graph(graph_id: str) -> GraphInfoResponse:
        stored = svc.store.get(graph_id)
        return GraphInfoResponse(
            graph_id=graph_id,
            node_count=stored.graph.node_count,
            output=stored.graph.output,
            layout=stored.graph.layout.to_dict(),
            point_version=stored.point_version,
            has_stored_point=stored.point is not None,
        )

    @app.delete("/graphs/{graph_id}", status_code=204)
    async def delete_graph(graph_id: str) -> None:
        svc.delete_graph(graph_id)

    @app.post("/graphs/{graph_id}/point", response_model=SetPointResponse)
    async def set_point(graph_id: str, req: SetPointRequest) -> SetPointResponse:
        version = svc.set_point(graph_id, req.point)
        return SetPointResponse(graph_id=graph_id, point_version=version)

    @app.post("/graphs/{graph_id}/hvp", response_model=HvpResponse)
    async def hvp(graph_id: str, req: HvpRequest) -> HvpResponse:
        result = svc.hvp(
            graph_id,
            HvpRequestData(
                vector=req.vector,
                point=req.point,
                use_stored_point=req.use_stored_point,
                expected_version=req.expected_version,
                nonsmooth_policy=req.nonsmooth_policy,
                subgradient=req.subgradient,
                kink_atol=req.kink_atol,
                max_eval_nodes=req.max_eval_nodes,
                time_budget_ms=req.time_budget_ms,
            ),
        )
        graph = svc.get_graph(graph_id)
        return HvpResponse(
            run_id=result.run_id,
            graph_id=result.graph_id,
            value=result.value,
            gradient=result.gradient,
            hvp=result.hvp,
            layout=graph.layout.to_dict(),
            diagnostics=result.diagnostics,
        )

    return app


app = create_app()
