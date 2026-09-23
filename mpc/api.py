"""FastAPI application: pure JSON backend for the bounded double-integrator MPC.

Endpoints
----------
GET  /health                 liveness + config defaults
POST /api/mpc/solve          single MPC solve from state + reference window
POST /api/mpc/rollout        deterministic closed-loop simulation

A new controller is built per request from explicit config fields, so the
service is stateless and requests cannot contaminate each other. The
test-only failure-injection hook is deliberately NOT exposed over HTTP.
"""

from __future__ import annotations

from dataclasses import replace

import numpy as np
from fastapi import FastAPI, HTTPException

from .config import MPCConfig
from .disturbance import DisturbanceSpec
from .mpc import MPCController
from .schemas import RolloutRequest, RolloutResponse, SolveRequest
from .simulation import constant_reference, rollout, step_reference

app = FastAPI(
    title="Bounded Double-Integrator MPC",
    version="1.0.0",
    description="Offline bounded MPC backend (OSQP) with verified fallback.",
)

_DEFAULT = MPCConfig()


def _build_controller(req: RolloutRequest) -> MPCController:
    overrides = {
        k: v for k, v in {
            "dt": req.dt,
            "horizon": req.horizon,
            "v_max": req.v_max,
            "a_max": req.a_max,
            "q_pos": req.q_pos,
            "q_vel": req.q_vel,
            "q_term_pos": req.q_term_pos,
            "q_term_vel": req.q_term_vel,
            "r_delta": req.r_delta,
            "k_brake": req.k_brake,
            "osqp_time_limit": req.osqp_time_limit,
        }.items() if v is not None
    }
    cfg = replace(_DEFAULT, **overrides)
    return MPCController(cfg)


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "service": "bounded-mpc",
        "defaults": {
            "dt": _DEFAULT.dt,
            "horizon": _DEFAULT.horizon,
            "v_max": _DEFAULT.v_max,
            "a_max": _DEFAULT.a_max,
            "osqp_time_limit": _DEFAULT.osqp_time_limit,
        },
    }


@app.post("/api/mpc/solve")
def solve_mpc(req: SolveRequest) -> dict:
    controller = MPCController()
    x0 = np.array(req.state, dtype=float)
    ref = np.array(req.reference, dtype=float)
    if ref.shape != (controller.config.horizon + 1, 2):
        raise HTTPException(
            status_code=422,
            detail=(
                f"reference must have shape "
                f"({controller.config.horizon + 1}, 2), got {list(ref.shape)}"
            ),
        )
    if not np.all(np.isfinite(x0)) or not np.all(np.isfinite(ref)):
        raise HTTPException(status_code=422, detail="all values must be finite")
    result = controller.solve(x0, ref, u_prev=float(req.u_prev))
    return result.as_dict()


@app.post("/api/mpc/rollout", response_model=RolloutResponse)
def rollout_mpc(req: RolloutRequest) -> RolloutResponse:
    if req.reference_setpoint is None and req.reference_after is None:
        raise HTTPException(
            status_code=422,
            detail="provide reference_setpoint or reference_before/after",
        )
    if req.reference_after is not None and req.change_step is None:
        raise HTTPException(
            status_code=422,
            detail="change_step is required with reference_before/after",
        )

    controller = _build_controller(req)
    cfg = controller.config
    n = req.n_steps

    if req.reference_setpoint is not None:
        base = constant_reference(tuple(req.reference_setpoint), n + cfg.horizon)
        windows = [base[k:k + cfg.horizon + 1] for k in range(n)]
    else:
        before = req.reference_before or [0.0, 0.0]
        windows = step_reference(
            tuple(before), tuple(req.reference_after),
            change_step=req.change_step,
            n_steps=n, horizon=cfg.horizon,
        )

    amp = (tuple(req.disturbance_amplitude)
           if isinstance(req.disturbance_amplitude, list)
           else float(req.disturbance_amplitude))
    spec = DisturbanceSpec(
        kind=req.disturbance_kind,
        amplitude=amp,
        frequency=req.disturbance_frequency,
        phase=req.disturbance_phase,
        step=req.disturbance_step,
    )

    result = rollout(controller, np.array(req.x0, dtype=float),
                     windows, n, disturbance=spec)
    payload = result.as_dict()
    payload["config"] = {
        "dt": cfg.dt, "horizon": cfg.horizon,
        "v_max": cfg.v_max, "a_max": cfg.a_max,
        "q_pos": cfg.q_pos, "q_vel": cfg.q_vel,
        "q_term_pos": cfg.q_term_pos, "q_term_vel": cfg.q_term_vel,
        "r_delta": cfg.r_delta, "k_brake": cfg.k_brake,
        "osqp_time_limit": cfg.osqp_time_limit,
    }
    return RolloutResponse(**payload)
