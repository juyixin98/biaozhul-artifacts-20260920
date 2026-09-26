"""JSON entry point for the 2D scan-matching library.

Usage:
    python -m scan_matching.cli request.json
    cat request.json | python -m scan_matching.cli -

The request is a JSON object with either explicit point clouds or a
synthetic scenario descriptor:

Explicit clouds:
    {
      "source": [[x, y], ...],
      "target": [[x, y], ...],
      "initial_pose": {"x": 0.0, "y": 0.0, "theta": 0.0},   // optional
      "params": {"max_iterations": 50, ...}                  // optional
    }

Synthetic scenario (points are generated, ground truth is reported):
    {
      "scenario": {
        "scene": "room",                // "room" | "line" | "corridor"
        "source_pose":  {"x": 0.3, "y": -0.1, "theta": 0.05},
        "target_pose":  {"x": 0.0, "y": 0.0,  "theta": 0.0},
        "noise_sigma": 0.01,
        "outliers": 20,                 // outliers added to the source
        "seed": 42
      },
      "initial_pose": {...}, "params": {...}                 // optional
    }

The response is a JSON envelope on stdout:
    {"success": true,  "result": {...}}
    {"success": false, "error": {"type": "...", "message": "..."}}
"""

from __future__ import annotations

import json
import sys

import numpy as np

from scan_matching.geometry import SE2Pose, compose, inverse, pose_error
from scan_matching.icp import ICPParams, icp
from scan_matching.synthetic import add_outliers, generate_scene, generate_scan


def _build_from_scenario(scenario: dict) -> tuple[np.ndarray, np.ndarray, dict]:
    """Generate source/target clouds from a scenario descriptor.

    Returns (source, target, ground_truth) where ground_truth holds the
    true relative pose aligning source onto target.
    """
    seed = int(scenario.get("seed", 0))
    rng = np.random.default_rng(seed)
    scene = generate_scene(scenario.get("scene", "room"))
    source_pose = SE2Pose.from_dict(scenario.get("source_pose", {}))
    target_pose = SE2Pose.from_dict(scenario.get("target_pose", {}))
    noise_sigma = float(scenario.get("noise_sigma", 0.01))
    max_range = float(scenario.get("max_range", 8.0))

    source = generate_scan(scene, source_pose, max_range=max_range,
                           noise_sigma=noise_sigma, rng=rng)
    target = generate_scan(scene, target_pose, max_range=max_range,
                           noise_sigma=noise_sigma, rng=rng)
    outlier_count = int(scenario.get("outliers", 0))
    if outlier_count > 0:
        source = add_outliers(source, outlier_count, rng)

    # scan_i lives in sensor frame i: p_world = T_i * p_i, so the pose
    # aligning source onto target is T_target^-1 * T_source.
    truth = compose(inverse(target_pose), source_pose)
    return source, target, {"pose": truth.to_dict()}


def run_request(request: dict) -> dict:
    """Execute one scan-matching request and return the result dict."""
    if not isinstance(request, dict):
        raise ValueError("request must be a JSON object")

    ground_truth = None
    if "scenario" in request:
        source, target, ground_truth = _build_from_scenario(request["scenario"])
    else:
        if "source" not in request or "target" not in request:
            raise ValueError("request needs either 'scenario' or both "
                             "'source' and 'target' point clouds")
        source = np.asarray(request["source"], dtype=float)
        target = np.asarray(request["target"], dtype=float)

    initial_pose = SE2Pose.from_dict(request.get("initial_pose", {}))
    params = ICPParams.from_dict(request.get("params"))

    result = icp(source, target, initial_pose=initial_pose, params=params)
    output = result.to_dict()
    output["num_source_points"] = int(len(source))
    output["num_target_points"] = int(len(target))
    if ground_truth is not None:
        output["ground_truth"] = ground_truth
        output["pose_error"] = pose_error(result.pose,
                                          SE2Pose.from_dict(ground_truth["pose"]))
    return output


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1:
        print("usage: python -m scan_matching.cli <request.json | ->",
              file=sys.stderr)
        return 2
    try:
        raw = sys.stdin.read() if argv[0] == "-" else _read_file(argv[0])
        request = json.loads(raw)
        response = {"success": True, "result": run_request(request)}
        exit_code = 0
    except (ValueError, KeyError, OSError) as exc:
        response = {"success": False,
                    "error": {"type": type(exc).__name__, "message": str(exc)}}
        exit_code = 1
    # allow_nan=False guarantees strict-JSON output (no Infinity/NaN).
    json.dump(response, sys.stdout, indent=2, allow_nan=False)
    sys.stdout.write("\n")
    return exit_code


def _read_file(path: str) -> str:
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


if __name__ == "__main__":
    raise SystemExit(main())
