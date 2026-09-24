"""FastAPI application: stateless bounded-MPC backend (JSON over HTTP)."""

from __future__ import annotations

import dataclasses

import osqp
from fastapi import FastAPI

from . import __version__
from .mpc import BoundedMPC, MpcConfig
from .schemas import SimulateRequest, SolveRequest
from .simulation import simulate

app = FastAPI(
    title="Bounded MPC backend (double integrator)",
    version=__version__,
    description="Stateless OSQP-based MPC: verified optimal control or an "
    "explicit conservative fallback with a reason.",
)


def _controller(req) -> BoundedMPC:
    return BoundedMPC(req.config.build() if req.config is not None else MpcConfig())


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "service": "bounded-mpc",
        "version": __version__,
        "osqp": osqp.__version__,
    }


@app.get("/matrices")
def matrices() -> dict:
    """Expose the default model/weight/constraint matrices for auditing."""
    return BoundedMPC().matrices()


@app.post("/solve")
def solve(req: SolveRequest) -> dict:
    """One MPC step; solver failure never raises — it returns a fallback."""
    mpc = _controller(req)
    result = mpc.solve(req.state, req.reference, req.u_prev)
    payload = result.as_dict()
    payload["config"] = dataclasses.asdict(mpc.cfg)
    return payload


@app.post("/simulate")
def simulate_endpoint(req: SimulateRequest) -> dict:
    """Deterministic closed-loop simulation with synthetic disturbance."""
    mpc = _controller(req)
    return simulate(
        mpc,
        req.initial_state,
        req.reference,
        req.steps,
        disturbance_amplitude=req.disturbance_amplitude,
        disturbance_seed=req.disturbance_seed,
    )
