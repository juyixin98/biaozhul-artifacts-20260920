"""FastAPI application exposing the SE(2) pose graph optimizer.

Endpoints
----------
* ``GET  /health``                 -- liveness probe
* ``POST /optimize``               -- run pose graph optimization
* ``GET  /``                       -- service description

Run with:

    uvicorn app:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

from typing import List, Optional

import numpy as np
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, field_validator

from pgo import Edge, PoseGraph, OptimizeOptions, KERNELS, optimize

app = FastAPI(
    title="SE(2) Pose Graph Optimization",
    version="1.0.0",
    description=(
        "Backend service for robust sparse 2-D pose graph optimization. "
        "See /docs for the interactive schema."
    ),
)


class EdgeIn(BaseModel):
    i: int = Field(..., description="source node index")
    j: int = Field(..., description="target node index")
    measurement: List[float] = Field(
        ...,
        min_length=3,
        max_length=3,
        description="relative pose (dx, dy, dtheta_rad) from i to j",
    )
    information: List[List[float]] = Field(
        ...,
        description="3x3 symmetric positive-definite information matrix",
    )

    @field_validator("information")
    @classmethod
    def _check_shape(cls, v):
        if len(v) != 3 or any(len(row) != 3 for row in v):
            raise ValueError("information must be a 3x3 matrix")
        return v


class OptionsIn(BaseModel):
    max_iterations: int = 30
    kernel: str = "none"
    kernel_delta: float = 1.0
    lm_init: float = 1e-6
    convergence_cost_tol: float = 1e-10
    convergence_step_tol: float = 1e-9
    gradient_tol: float = 1e-8
    fixed_nodes: Optional[List[int]] = Field(
        default=None,
        description="nodes held fixed; defaults to [0] to remove the global SE(2) gauge",
    )

    @field_validator("kernel")
    @classmethod
    def _check_kernel(cls, v):
        if v not in KERNELS:
            raise ValueError(f"kernel must be one of {KERNELS}")
        return v


class OptimizeRequest(BaseModel):
    poses: List[List[float]] = Field(
        ..., description="initial poses, one (x, y, theta_rad) triple per node"
    )
    edges: List[EdgeIn]
    options: OptionsIn = Field(default_factory=OptionsIn)

    @field_validator("poses")
    @classmethod
    def _check_poses(cls, v):
        if not v:
            raise ValueError("at least one pose is required")
        if any(len(p) != 3 for p in v):
            raise ValueError("each pose must be (x, y, theta)")
        return v


class IterationOut(BaseModel):
    iteration: int
    cost: float
    gradient_inf_norm: float
    step_inf_norm: float
    damping: float
    accepted: bool


class OptimizeResponse(BaseModel):
    poses: List[List[float]]
    initial_cost: float
    final_cost: float
    initial_gradient_inf_norm: float
    final_gradient_inf_norm: float
    iterations: int
    converged: str
    degenerate: bool
    connected_components: int
    min_eigenvalue: Optional[float]
    max_eigenvalue: Optional[float]
    edge_costs: List[float]
    history: List[IterationOut]
    message: str
    num_nodes: int
    num_edges: int


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/")
def root():
    return {
        "service": "se2-pose-graph-optimizer",
        "endpoints": ["/health", "/optimize", "/docs", "/openapi.json"],
    }


@app.post("/optimize", response_model=OptimizeResponse)
def post_optimize(req: OptimizeRequest):
    try:
        poses = np.asarray(req.poses, dtype=float)
        edges = [
            Edge(
                i=e.i,
                j=e.j,
                measurement=np.asarray(e.measurement, dtype=float),
                information=np.asarray(e.information, dtype=float),
            )
            for e in req.edges
        ]
        graph = PoseGraph(poses, edges)
        opts = OptimizeOptions(
            max_iterations=req.options.max_iterations,
            kernel=req.options.kernel,
            kernel_delta=req.options.kernel_delta,
            lm_init=req.options.lm_init,
            convergence_cost_tol=req.options.convergence_cost_tol,
            convergence_step_tol=req.options.convergence_step_tol,
            gradient_tol=req.options.gradient_tol,
            fixed_nodes=req.options.fixed_nodes,
        )
        result = optimize(graph, opts)
    except ValueError as exc:
        # bad user input (bad indices, non-PD information, ...) -> 422
        raise HTTPException(status_code=422, detail=str(exc))

    return OptimizeResponse(
        poses=result.poses.tolist(),
        initial_cost=result.initial_cost,
        final_cost=result.final_cost,
        initial_gradient_inf_norm=result.initial_gradient_inf_norm,
        final_gradient_inf_norm=result.final_gradient_inf_norm,
        iterations=result.iterations,
        converged=result.converged,
        degenerate=result.degenerate,
        connected_components=result.connected_components,
        min_eigenvalue=result.min_eigenvalue,
        max_eigenvalue=result.max_eigenvalue,
        edge_costs=result.edge_costs.tolist(),
        history=[
            IterationOut(
                iteration=h.iteration,
                cost=h.cost,
                gradient_inf_norm=h.gradient_inf_norm,
                step_inf_norm=h.step_inf_norm,
                damping=h.damping,
                accepted=h.accepted,
            )
            for h in result.history
        ],
        message=result.message,
        num_nodes=graph.num_nodes,
        num_edges=len(graph.edges),
    )
