"""FastAPI application: tensor/graph/training/validation endpoints.

All responses are an envelope ``{request_id, ...}`` and failures carry an
explicit ``failure_category`` drawn from
:mod:`tensor_backend.tensor.errors`.  Nothing here needs external services;
request payloads are local synthetic data.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..config import SETTINGS, SERVICE_NAME, SERVICE_VERSION
from ..graph import ComputeGraph
from ..tensor import Tensor
from ..tensor.errors import TensorError
from ..tensor.layout import normalize_shape
from ..training import TrainingState
from ..validation import run_validation_suite
from .logging_setup import configure_logging, log_event

logger = configure_logging()

app = FastAPI(title=SERVICE_NAME, version=SERVICE_VERSION)

# In-process graph sessions (local backend; one process, no external store).
_GRAPHS: dict[str, ComputeGraph] = {}


# --------------------------------------------------------------- middleware

@app.middleware("http")
async def correlate_requests(request: Request, call_next):
    request_id = request.headers.get("x-request-id") or f"req-{uuid.uuid4().hex[:12]}"
    request.state.request_id = request_id
    log_event(logger, f"{request.method} {request.url.path}",
              request_id=request_id, location="http.request")
    response = await call_next(request)
    response.headers["x-request-id"] = request_id
    response.headers["x-service-version"] = SERVICE_VERSION
    return response


# ----------------------------------------------------------------- schemas

class Step(BaseModel):
    op: str
    out: str | None = None
    src: str | None = None
    dst: str | None = None
    a: str | None = None
    b: str | None = None
    into: str | None = None
    axes: list[int] | None = None
    start: list[int | None] | None = None
    stop: list[int | None] | None = None
    step: list[int | None] | None = None
    shape: list[int] | None = None
    values: Any | None = None
    axis: int | None = None
    allow_copy: bool = False
    overlap_policy: str = "raise"


class GraphRequest(BaseModel):
    graph_id: str | None = None
    constants: dict[str, Any] = Field(default_factory=dict)
    steps: list[Step] = Field(default_factory=list)


class ReshapeCheckRequest(BaseModel):
    shape: list[int]
    strides: list[int]
    new_shape: list[int]
    offset: int = 0
    storage_size: int | None = None


class LinearTrainRequest(BaseModel):
    x: list[list[float]]
    y: list[list[float]]
    steps: int = 20
    learning_rate: float = 0.05
    seed: int = 0


# ------------------------------------------------------------------ helpers

def _error_response(request_id: str, exc: TensorError, *, status: int = 422) -> JSONResponse:
    log_event(
        logger, "operation failed",
        request_id=request_id, location="tensor_error",
        level=logging.WARNING, failure_category=getattr(exc, "category", "tensor_error"),
    )
    return JSONResponse(
        status_code=status,
        content={
            "request_id": request_id,
            "ok": False,
            "failure_category": getattr(exc, "category", "tensor_error"),
            "error": str(exc),
        },
    )


def _tensor_payload(t: Tensor) -> dict[str, Any]:
    lo, hi = t.storage_range
    overlaps, uncertain = t.self_overlap()
    return {
        **t.describe(),
        "values": t.to_list(),
        "self_overlapping": overlaps,
        "overlap_uncertain": uncertain,
        "storage_interval": [lo, hi],
    }


# ---------------------------------------------------------------- endpoints

@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "service": SERVICE_NAME,
        "version": SERVICE_VERSION,
        "ok": True,
        "max_ndim": SETTINGS.max_ndim,
        "max_elements": SETTINGS.max_elements,
    }


@app.get("/ops")
def list_ops() -> dict[str, Any]:
    from ..tensor import ops as tops
    return {
        "binary": tops.list_binary_ops(),
        "unary": tops.list_unary_ops(),
        "overlap_policies": ["raise", "temp"],
    }


@app.post("/graphs")
def create_graph(body: GraphRequest, request: Request) -> JSONResponse:
    request_id = request.state.request_id
    graph = ComputeGraph(graph_id=body.graph_id)
    try:
        for handle, values in body.constants.items():
            graph.constant(handle, values)
        for step in body.steps:
            graph.execute_plan({"steps": [step.model_dump(exclude_none=True)]})
    except TensorError as exc:
        return _error_response(request_id, exc)
    except (KeyError, ValueError) as exc:
        log_event(logger, "bad graph request", request_id=request_id,
                  location="graph", level=logging.WARNING)
        return JSONResponse(status_code=400, content={
            "request_id": request_id, "ok": False,
            "failure_category": "bad_request", "error": str(exc),
        })

    _GRAPHS[graph.graph_id] = graph
    outputs: dict[str, Any] = {}
    for handle in graph.handles():
        outputs[handle] = _tensor_payload(graph.tensor(handle))
    log_event(logger, f"graph {graph.graph_id} executed {len(body.steps)} steps",
              request_id=request_id, location="graph.execute",
              steps=len(body.steps), graph_id=graph.graph_id)
    return JSONResponse(content={
        "request_id": request_id,
        "ok": True,
        "graph_id": graph.graph_id,
        "tensors": outputs,
        "trace": graph.trace(),
        "aliasing": graph.aliasing_report(),
    })


@app.get("/graphs/{graph_id}/tensors/{handle}")
def get_tensor(graph_id: str, handle: str, request: Request) -> JSONResponse:
    request_id = request.state.request_id
    graph = _GRAPHS.get(graph_id)
    if graph is None:
        return JSONResponse(status_code=404, content={
            "request_id": request_id, "ok": False,
            "failure_category": "not_found", "error": f"no graph {graph_id!r}",
        })
    try:
        t = graph.tensor(handle)
    except KeyError as exc:
        return JSONResponse(status_code=404, content={
            "request_id": request_id, "ok": False,
            "failure_category": "not_found", "error": str(exc),
        })
    return JSONResponse(content={
        "request_id": request_id, "ok": True,
        "graph_id": graph_id, "tensor": _tensor_payload(t),
    })


@app.post("/reshape/check")
def reshape_check(body: ReshapeCheckRequest, request: Request) -> JSONResponse:
    """Report copy/zero-copy feasibility of an arbitrary layout — no graph."""
    request_id = request.state.request_id
    try:
        shape = normalize_shape(body.shape, SETTINGS.max_elements)
        t = Tensor.from_layout(
            [0.0] * (body.storage_size or _safe_storage_size(shape, body.strides, body.offset)),
            shape=shape,
            strides=body.strides,
            offset=body.offset,
        )
        info = t.reshape_info(body.new_shape)
    except TensorError as exc:
        return _error_response(request_id, exc)
    return JSONResponse(content={"request_id": request_id, "ok": True, **info})


def _safe_storage_size(shape, strides, offset) -> int:
    # Upper bound covering every sign combination of the strides: the furthest
    # reachable index is offset + sum((d-1)*|stride|).
    return offset + sum((d - 1) * abs(s) for d, s in zip(shape, strides)) + 1


@app.post("/validate")
def validate(request: Request) -> JSONResponse:
    request_id = request.state.request_id
    report = run_validation_suite()
    failed = report["summary"]["failed"] or report["summary"]["errors"]
    uncertain = report["summary"]["uncertain"]
    log_event(
        logger,
        f"validation suite: {report['summary']}",
        request_id=request_id, location="validation",
        level=logging.WARNING if failed else logging.INFO,
        failure_category="validation_failed" if failed else None,
        uncertain=uncertain or None,
    )
    return JSONResponse(content={
        "request_id": request_id,
        "ok": True,
        "oracle": "numpy",
        "oracle_version": _numpy_version(),
        "summary": report["summary"],
        "checks": report["checks"],
        "failures": [c for c in report["checks"] if c["status"] in ("fail", "error")],
        "uncertainties": [c for c in report["checks"] if c["status"] == "uncertain"],
    })


@app.post("/train/linear")
def train_linear(body: LinearTrainRequest, request: Request) -> JSONResponse:
    request_id = request.state.request_id
    try:
        x = Tensor.from_values(body.x, name="X")
        y = Tensor.from_values(body.y, name="y")
        if x.shape[0] != y.shape[0] or y.ndim != 2 or y.shape[1] != 1:
            raise TensorError("y must have shape (n,1) and align with X rows")
        state = TrainingState(learning_rate=body.learning_rate)
        state.init_linear(x.shape[1], seed=body.seed)
        losses = state.train(x, y, body.steps, request_id=request_id)
    except TensorError as exc:
        return _error_response(request_id, exc)
    log_event(logger, f"linear training {body.steps} steps -> version {state.version}",
              request_id=request_id, location="training.train",
              state_id=state.state_id)
    snap = state.snapshot()
    return JSONResponse(content={
        "request_id": request_id,
        "ok": True,
        "losses": losses,
        "final_version": state.version,
        "state": snap,
    })


def _numpy_version() -> str:
    import numpy
    return numpy.__version__
