"""FastAPI application exposing point-to-point ICP registration.

Endpoints
----------
GET  /health                 liveness probe
POST /api/icp                register two point clouds
POST /api/synthetic-scene    generate a synthetic transformed cloud with truth
POST /api/demo               run a built-in acceptance-style scenario
"""

from __future__ import annotations

import numpy as np
from fastapi import FastAPI
from fastapi.concurrency import run_in_threadpool

from .icp_core import (
    icp,
    rotation_error_deg,
    translation_error,
    validate_rotation,
)
from .schemas import DemoRequest, ICPRequest, SceneRequest
from .synthetic import make_scene, rotation_about_axis

app = FastAPI(
    title="Point-to-Point ICP Registration",
    version="0.1.0",
    description=(
        "Synthetic/offline rigid registration backend. ICP is a LOCAL "
        "optimizer; responses never assert global optimality."
    ),
)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/icp")
async def run_icp(req: ICPRequest) -> dict:
    source = np.asarray(req.source, dtype=float)
    target = np.asarray(req.target, dtype=float)
    R0 = None if req.R0 is None else np.asarray(req.R0, dtype=float)
    t0 = None if req.t0 is None else np.asarray(req.t0, dtype=float)

    # CPU-bound NumPy/SciPy work: keep the event loop free.
    result = await run_in_threadpool(
        icp,
        source,
        target,
        R0,
        t0,
        max_iterations=req.max_iterations,
        tolerance=req.tolerance,
        max_correspondence_distance=req.max_correspondence_distance,
        robust_quantile=req.robust_quantile,
        min_inliers=req.min_inliers,
        degeneracy_ratio=req.degeneracy_ratio,
    )
    return result.to_dict()


@app.post("/api/synthetic-scene")
async def synthetic_scene(req: SceneRequest) -> dict:
    scene = await run_in_threadpool(
        make_scene,
        n_points=req.n_points,
        kind=req.kind,
        angle_deg=req.angle_deg,
        translation=req.translation,
        noise_std=req.noise_std,
        overlap=req.overlap,
        n_clutter=req.n_clutter,
        seed=req.seed,
    )
    return {
        "source": scene.source.tolist(),
        "target": scene.target.tolist(),
        "R_true": scene.R_true.tolist(),
        "t_true": scene.t_true.tolist(),
        "overlap": scene.overlap,
        "noise_std": scene.noise_std,
        "kind": scene.kind,
    }


def _collinear_demo(seed: int) -> dict:
    rng = np.random.default_rng(seed)
    n = 80
    s = rng.uniform(-1.0, 1.0, size=n)
    source = np.zeros((n, 3))
    source[:, 0] = s

    # With the true initial guess the nearest-neighbour pairing is correct
    # and ICP aligns the line; from a distant guess collinear NN pairing is
    # ambiguous and ICP would fall into a wrong local minimum (a genuine
    # limitation, surfaced by the collinear-degeneracy warning).
    R_true = rotation_about_axis([0.0, 0.0, 1.0], 30.0)
    t_true = np.array([0.4, -0.3, 0.1])
    target = (R_true @ source.T).T + t_true  # exact, noise-free

    result = icp(source, target, R_true, t_true,
                 max_iterations=100, robust_quantile=1.0)
    return {
        "description": (
            "Noise-free collinear cloud aligned from a good initial guess. "
            "The line is recovered exactly, but rotation about it is "
            "unobservable and reported as degenerate. From a distant guess, "
            "nearest-neighbour pairing on a line is ambiguous and ICP can "
            "converge to a wrong local minimum."
        ),
        "result": result.to_dict(),
        "R_true": R_true.tolist(),
        "t_true": t_true.tolist(),
        "rotation_error_deg": rotation_error_deg(result.R, R_true),
        "translation_error": translation_error(result.t, t_true),
    }


def _demo_common(scene_kwargs: dict, icp_kwargs: dict, R0=None, t0=None) -> tuple:
    scene = make_scene(**scene_kwargs)
    result = icp(scene.source, scene.target, R0, t0, **icp_kwargs)
    return scene, result


@app.post("/api/demo")
async def demo(req: DemoRequest) -> dict:
    if req.scenario == "nominal":
        scene, result = _demo_common(
            dict(n_points=150, kind="volume", angle_deg=20.0,
                 translation=0.5, noise_std=0.01, seed=req.seed),
            dict(max_iterations=100, robust_quantile=1.0),
        )
        description = "Full-overlap 3D cloud, noise 0.01, identity initial guess."
    elif req.scenario == "partial_overlap":
        scene, result = _demo_common(
            dict(n_points=200, kind="volume", angle_deg=15.0,
                 translation=0.3, noise_std=0.01, overlap=0.6,
                 n_clutter=60, seed=req.seed),
            dict(max_iterations=100, robust_quantile=0.7,
                 max_correspondence_distance=1.5),
        )
        description = "60% overlap plus 60 clutter points; trimmed ICP."
    elif req.scenario == "collinear":
        return _collinear_demo(req.seed)
    elif req.scenario == "bad_initial":
        scene = make_scene(
            n_points=150, kind="clusters", angle_deg=25.0,
            translation=0.2, noise_std=0.005, seed=req.seed,
        )
        # Deliberately wrong initial guess: rotate ~140 deg away from truth.
        R0 = scene.R_true @ rotation_about_axis([0.0, 0.0, 1.0], 140.0)
        validate_rotation(R0)
        result = icp(scene.source, scene.target, R0, scene.t_true,
                     max_iterations=100, robust_quantile=1.0)
        description = (
            "Initial guess ~140 deg off; demonstrates that ICP may converge "
            "to a local minimum. Must not be reported as the global optimum."
        )
    else:  # nonconverge
        scene, result = _demo_common(
            dict(n_points=150, kind="volume", angle_deg=60.0,
                 translation=1.0, noise_std=0.01, seed=req.seed),
            dict(max_iterations=2, robust_quantile=1.0),
        )
        description = "Large misalignment with only 2 iterations; non-convergence is reported."

    return {
        "description": description,
        "result": result.to_dict(),
        "R_true": scene.R_true.tolist(),
        "t_true": scene.t_true.tolist(),
        "rotation_error_deg": rotation_error_deg(result.R, scene.R_true),
        "translation_error": translation_error(result.t, scene.t_true),
    }
