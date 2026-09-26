"""JSON request/response layer for the ICP command-line entry point.

Request schema (all lengths in metres, angles in radians)::

    {
      "source": [[x, y], ...],            // required, >= 3 points
      "target": [[x, y], ...],            // required, >= 3 points
      "initial_guess": [tx, ty, theta],   // optional, defaults to identity
      "config": { ... }                   // optional, subset of ICPConfig fields
    }

The response mirrors the ICPResult: pose, convergence status, per-iteration
residuals, covariance and the degeneracy/uncertainty report. The top-level
``success`` flag is false only when no usable estimate could be produced
(e.g. too few inliers); a degenerate-but-solved problem still returns
``success: true`` with ``degeneracy.degenerate: true``.
"""

from __future__ import annotations

import json

import numpy as np

from .icp import ICPConfig, ICPResult, estimate_pose

CONFIG_FIELDS = set(ICPConfig.__dataclass_fields__)


def load_request(payload: str | dict) -> dict:
    """Parse and validate a JSON request (string or already-parsed dict)."""
    if isinstance(payload, str):
        try:
            request = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ValueError(f"request is not valid JSON: {exc}") from exc
    else:
        request = payload
    if not isinstance(request, dict):
        raise ValueError("request must be a JSON object")
    for key in ("source", "target"):
        if key not in request:
            raise ValueError(f"request is missing required field {key!r}")
        _validate_points(request[key], key)
    if "initial_guess" in request:
        guess = request["initial_guess"]
        if (
            not isinstance(guess, (list, tuple))
            or len(guess) != 3
            or not all(isinstance(v, (int, float)) for v in guess)
        ):
            raise ValueError("initial_guess must be [tx, ty, theta]")
    if "config" in request:
        if not isinstance(request["config"], dict):
            raise ValueError("config must be a JSON object")
        unknown = set(request["config"]) - CONFIG_FIELDS
        if unknown:
            raise ValueError(
                f"unknown config field(s): {sorted(unknown)}; "
                f"allowed: {sorted(CONFIG_FIELDS)}"
            )
    return request


def run_request(request: dict) -> dict:
    """Validate a request, run ICP and return the response dict."""
    request = load_request(request)
    config = ICPConfig(**request.get("config", {}))
    result = estimate_pose(
        source=np.asarray(request["source"], dtype=float),
        target=np.asarray(request["target"], dtype=float),
        initial_pose=(
            None
            if "initial_guess" not in request
            else np.asarray(request["initial_guess"], dtype=float)
        ),
        config=config,
    )
    return result_to_response(result)


def result_to_response(result: ICPResult) -> dict:
    """Serialize an ICPResult into the JSON response schema."""
    degeneracy = result.degeneracy
    return {
        "success": result.status != "insufficient_inliers",
        "pose": {
            "x": float(result.pose[0]),
            "y": float(result.pose[1]),
            "theta": float(result.pose[2]),
        },
        "status": result.status,
        "converged": result.converged,
        "uncertain": result.uncertain,
        "iterations": result.iterations,
        "final_rmse": _finite_or_none(result.final_rmse),
        "residual_history": [float(r) for r in result.residual_history],
        "num_inliers": result.num_inliers,
        "num_source_points": result.num_source_points,
        "covariance": _matrix_or_none(result.covariance),
        "hessian_eigenvalues": _vector_or_none(result.hessian_eigenvalues),
        "degeneracy": {
            "degenerate": degeneracy.degenerate,
            "flat_translation_direction": degeneracy.flat_translation_direction,
            "rotation_flat": degeneracy.rotation_flat,
            "translation_flatness_ratio": _finite_or_none(
                degeneracy.translation_flatness_ratio
            ),
            "min_translation_cost_increase": _finite_or_none(
                degeneracy.min_translation_cost_increase
            ),
            "max_translation_cost_increase": _finite_or_none(
                degeneracy.max_translation_cost_increase
            ),
            "rotation_cost_increase": _finite_or_none(
                degeneracy.rotation_cost_increase
            ),
            "rotation_flatness_ratio": _finite_or_none(
                degeneracy.rotation_flatness_ratio
            ),
            "hessian_eigenvalue_ratio": _finite_or_none(
                degeneracy.hessian_eigenvalue_ratio
            ),
            "message": degeneracy.message,
        },
        "iteration_log": [
            {
                "iteration": record.iteration,
                "rmse": record.rmse,
                "num_inliers": record.num_inliers,
                "step_translation": record.step_translation,
                "step_rotation": record.step_rotation,
                "pose": record.pose,
            }
            for record in result.history
        ],
    }


def _validate_points(value, name: str) -> None:
    if not isinstance(value, list) or len(value) < 3:
        raise ValueError(f"{name} must be a list of at least 3 [x, y] points")
    for point in value:
        if (
            not isinstance(point, (list, tuple))
            or len(point) != 2
            or not all(isinstance(v, (int, float)) for v in point)
        ):
            raise ValueError(f"{name} entries must be [x, y] number pairs")


def _finite_or_none(value: float):
    return float(value) if np.isfinite(value) else None


def _matrix_or_none(matrix: np.ndarray):
    return matrix.tolist() if np.all(np.isfinite(matrix)) else None


def _vector_or_none(vector: np.ndarray):
    return [float(v) for v in vector] if np.all(np.isfinite(vector)) else None
