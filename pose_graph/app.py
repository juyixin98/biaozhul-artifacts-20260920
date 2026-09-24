"""FastAPI application exposing the SE(2) pose-graph optimizer over HTTP."""

from __future__ import annotations

import numpy as np
from fastapi import FastAPI, HTTPException

from .models import OptimizeRequest, OptimizeResponse
from .optimizer import Edge, OptimizeOptions, optimize

app = FastAPI(
    title="SE(2) Pose Graph Optimizer",
    version="0.1.0",
    description=(
        "Optimizes 2D pose graphs from relative-pose constraints with "
        "information matrices, a robust Huber kernel and a sparse solver. "
        "The first node is held fixed to remove the gauge freedom."
    ),
)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/optimize", response_model=OptimizeResponse)
def optimize_endpoint(request: OptimizeRequest) -> OptimizeResponse:
    n = len(request.initial_poses)
    for k, edge in enumerate(request.edges):
        if edge.i >= n or edge.j >= n:
            raise HTTPException(
                status_code=422,
                detail=f"edge {k} references node out of range [0, {n})",
            )
    if request.options.fix_node >= n:
        raise HTTPException(
            status_code=422,
            detail=f"fix_node {request.options.fix_node} out of range [0, {n})",
        )

    edges = [
        Edge(
            i=e.i,
            j=e.j,
            z=np.asarray(e.measurement, dtype=float),
            omega=np.asarray(e.information, dtype=float),
        )
        for e in request.edges
    ]
    options = OptimizeOptions(**request.options.model_dump())

    result = optimize(np.asarray(request.initial_poses, dtype=float), edges, options)

    return OptimizeResponse(
        success=True,
        message=result.message,
        optimized_poses=result.poses.tolist(),
        iterations=result.iterations,
        converged=result.converged,
        cost_history=result.cost_history,
        gradient_norm_history=result.gradient_norm_history,
        final_cost=result.final_cost,
        final_gradient_norm=result.final_gradient_norm,
        degeneracy=result.degeneracy,
    )
