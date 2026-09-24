"""Offline demo for point-to-point ICP.

Local mode (default):
    python -m scripts.demo
    python -m scripts.demo --scenario partial_overlap

HTTP mode (against a running `uvicorn app.main:app`):
    python -m scripts.demo --url http://127.0.0.1:8000 --scenario nominal

Scenarios: nominal | collinear | partial_overlap | bad_initial | nonconverge
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request

import numpy as np

from app.icp_core import icp, rotation_error_deg, translation_error
from app.synthetic import make_scene, rotation_about_axis


def build_local(scenario: str, seed: int) -> dict:
    if scenario == "nominal":
        scene = make_scene(n_points=150, kind="volume", angle_deg=20.0,
                           translation=0.5, noise_std=0.01, seed=seed)
        result = icp(scene.source, scene.target,
                     max_iterations=100, robust_quantile=1.0)
        R_true, t_true = scene.R_true, scene.t_true
        desc = "full-overlap 3D cloud, sigma=0.01, identity init"

    elif scenario == "collinear":
        rng = np.random.default_rng(seed)
        n = 80
        source = np.zeros((n, 3))
        source[:, 0] = rng.uniform(-1.0, 1.0, size=n)
        R_true = rotation_about_axis([0.0, 0.0, 1.0], 30.0)
        t_true = np.array([0.4, -0.3, 0.1])
        target = (R_true @ source.T).T + t_true
        result = icp(source, target, R_true, t_true,
                     max_iterations=100, robust_quantile=1.0)
        desc = ("noise-free collinear cloud aligned from a good initial guess "
                "(degenerate; distant guess converges to a wrong local min)")

    elif scenario == "partial_overlap":
        scene = make_scene(n_points=200, kind="volume", angle_deg=15.0,
                           translation=0.3, noise_std=0.01, overlap=0.6,
                           n_clutter=60, seed=seed)
        result = icp(scene.source, scene.target,
                     max_iterations=100, robust_quantile=0.7,
                     max_correspondence_distance=1.5)
        R_true, t_true = scene.R_true, scene.t_true
        desc = "60% overlap + 60 clutter points, trimmed ICP"

    elif scenario == "bad_initial":
        scene = make_scene(n_points=150, kind="clusters", angle_deg=25.0,
                           translation=0.2, noise_std=0.005, seed=seed)
        R0 = scene.R_true @ rotation_about_axis([0.0, 0.0, 1.0], 140.0)
        result = icp(scene.source, scene.target, R0, scene.t_true,
                     max_iterations=100, robust_quantile=1.0)
        R_true, t_true = scene.R_true, scene.t_true
        desc = "initial guess ~140 deg off (local-minimum probe)"

    elif scenario == "nonconverge":
        scene = make_scene(n_points=150, kind="volume", angle_deg=60.0,
                           translation=1.0, noise_std=0.01, seed=seed)
        result = icp(scene.source, scene.target,
                     max_iterations=2, robust_quantile=1.0)
        R_true, t_true = scene.R_true, scene.t_true
        desc = "large misalignment, 2-iteration budget (non-convergence probe)"
    else:
        raise ValueError(f"unknown scenario: {scenario}")

    return {
        "description": desc,
        "status": result.status.value,
        "iterations": result.iterations,
        "rmse": result.rmse,
        "inlier_count": result.inlier_count,
        "inlier_fraction": result.inlier_fraction,
        "degenerate": result.degeneracy.degenerate,
        "degeneracy_kind": result.degeneracy.kind,
        "reflections_rejected": result.reflections_rejected,
        "rotation_error_deg": rotation_error_deg(result.R, R_true),
        "translation_error": translation_error(result.t, t_true),
        "warnings": result.warnings,
        "rmse_history": result.history,
    }


def fetch_http(base_url: str, scenario: str, seed: int) -> dict:
    body = json.dumps({"scenario": scenario, "seed": seed}).encode()
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/api/demo",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read())
    r = payload["result"]
    return {
        "description": payload["description"],
        "status": r["status"],
        "iterations": r["iterations"],
        "rmse": r["rmse"],
        "inlier_count": r["inlier_count"],
        "inlier_fraction": r["inlier_fraction"],
        "degenerate": r["degeneracy"]["degenerate"],
        "degeneracy_kind": r["degeneracy"]["kind"],
        "reflections_rejected": r["reflections_rejected"],
        "rotation_error_deg": payload["rotation_error_deg"],
        "translation_error": payload["translation_error"],
        "warnings": r["warnings"],
        "rmse_history": r["history"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scenario",
        choices=["nominal", "collinear", "partial_overlap",
                 "bad_initial", "nonconverge"],
        default="nominal",
    )
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--url", default=None,
                        help="base URL of a running server; offline local run if omitted")
    args = parser.parse_args()

    if args.url:
        out = fetch_http(args.url, args.scenario, args.seed)
    else:
        out = build_local(args.scenario, args.seed)

    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
