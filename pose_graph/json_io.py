"""JSON request/response interface for the pose graph optimizer.

Request schema (see ``examples/`` for complete samples)::

    {
      "options": {"max_iterations": 100, "ftol": 1e-8, ...},
      "nodes": [{"id": 0, "pose": [x, y, theta], "fixed": true}, ...],
      "edges": [{
          "i": 0, "j": 1,
          "z": [dx, dy, dtheta],
          "information": [[..3x3..]],
          "kernel": {"type": "huber", "parameter": 1.0},
          "label": "odometry"
      }, ...]
    }

A successful response has ``"status": "ok"``; malformed input produces
``"status": "error"`` with a type and message.  The solver returns a *local*
optimum — global optimality is never guaranteed.
"""

import json

import numpy as np

from .graph import GraphStructureError, PoseGraph
from .optimizer import OptimizeOptions, optimize
from .se2 import wrap_angle

_OPTION_FIELDS = {
    "max_iterations": int,
    "ftol": float,
    "xtol": float,
    "gtol": float,
    "lambda_init": float,
    "strict_anchoring": bool,
}


def load_request(path):
    """Load a JSON request file."""
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def dump_response(response, path):
    """Write a response dict as indented JSON."""
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(response, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def _parse_options(raw):
    if raw is None:
        return OptimizeOptions()
    if not isinstance(raw, dict):
        raise GraphStructureError("'options' must be an object")
    kwargs = {}
    for key, caster in _OPTION_FIELDS.items():
        if key in raw:
            try:
                kwargs[key] = caster(raw[key])
            except (TypeError, ValueError) as exc:
                raise GraphStructureError(f"options.{key}: {exc}") from exc
    unknown = set(raw) - set(_OPTION_FIELDS)
    if unknown:
        raise GraphStructureError(f"unknown option(s): {sorted(unknown)}")
    return OptimizeOptions(**kwargs)


def build_graph_from_dict(data) -> PoseGraph:
    """Validate a request dict and construct the :class:`PoseGraph`."""
    if not isinstance(data, dict):
        raise GraphStructureError("request root must be a JSON object")
    graph = PoseGraph()

    nodes = data.get("nodes", [])
    if not isinstance(nodes, list) or not nodes:
        raise GraphStructureError("'nodes' must be a non-empty list")
    for node in nodes:
        if not isinstance(node, dict) or "id" not in node or "pose" not in node:
            raise GraphStructureError("each node needs 'id' and 'pose'")
        pose = node["pose"]
        if not isinstance(pose, list) or len(pose) != 3:
            raise GraphStructureError(
                f"node {node['id']}: 'pose' must be a list of 3 numbers"
            )
        graph.add_node(
            index=int(node["id"]),
            pose=[float(v) for v in pose],
            fixed=bool(node.get("fixed", False)),
        )

    edges = data.get("edges", [])
    if not isinstance(edges, list):
        raise GraphStructureError("'edges' must be a list")
    for edge in edges:
        for key in ("i", "j", "z", "information"):
            if key not in edge:
                raise GraphStructureError(f"edge missing field '{key}'")
        z = edge["z"]
        info = edge["information"]
        if not isinstance(z, list) or len(z) != 3:
            raise GraphStructureError(
                f"edge {edge['i']}->{edge['j']}: 'z' must be a list of 3 numbers"
            )
        if not isinstance(info, list) or len(info) != 3 or any(
            not isinstance(row, list) or len(row) != 3 for row in info
        ):
            raise GraphStructureError(
                f"edge {edge['i']}->{edge['j']}: 'information' must be 3x3"
            )
        kernel = edge.get("kernel", {"type": "linear"})
        if not isinstance(kernel, dict) or "type" not in kernel:
            raise GraphStructureError(
                f"edge {edge['i']}->{edge['j']}: kernel needs a 'type'"
            )
        graph.add_edge(
            i=int(edge["i"]),
            j=int(edge["j"]),
            z=[float(v) for v in z],
            information=np.array(info, dtype=float),
            kernel_type=str(kernel["type"]),
            kernel_parameter=(
                float(kernel["parameter"]) if kernel.get("parameter") is not None else None
            ),
            label=str(edge.get("label", "")),
        )
    return graph


def _result_to_dict(result):
    return {
        "status": "ok",
        "summary": {
            "num_nodes": len(result.poses),
            "num_edges": len(result.edge_stats),
            "cost_initial": result.cost_initial,
            "cost_final": result.cost_final,
            "cost_reduction_ratio": (
                (result.cost_initial - result.cost_final) / result.cost_initial
                if result.cost_initial > 0.0
                else 0.0
            ),
            "iterations": result.iterations,
            "converged": result.converged,
            "termination_reason": result.termination_reason,
            "fixed_nodes": result.fixed_nodes,
            "auto_anchored_nodes": result.auto_anchored_nodes,
            "rms_residual_initial": result.rms_residual_initial,
            "rms_residual_final": result.rms_residual_final,
            "max_angular_residual_initial": result.max_angular_residual_initial,
            "max_angular_residual_final": result.max_angular_residual_final,
        },
        "poses": [
            {
                "id": idx,
                "pose": [float(v) for v in result.poses[idx]],
                "theta_wrapped": float(wrap_angle(result.poses[idx][2])),
            }
            for idx in sorted(result.poses)
        ],
        "cost_history": result.cost_history,
        "connectivity": result.connectivity,
        "edge_stats": [
            {
                "i": s.i,
                "j": s.j,
                "label": s.label,
                "kernel": s.kernel_type,
                "squared_residual_initial": s.squared_residual_initial,
                "squared_residual_final": s.squared_residual_final,
                "robust_cost_initial": s.robust_cost_initial,
                "robust_cost_final": s.robust_cost_final,
                "angular_residual_abs_final": s.angular_residual_abs_final,
            }
            for s in result.edge_stats
        ],
        "note": "Local optimum from iterative nonlinear least squares; "
        "global optimality is not guaranteed.",
    }


def run_from_dict(data):
    """Execute a full optimization from a request dict; return response dict."""
    try:
        graph = build_graph_from_dict(data)
        options = _parse_options(data.get("options"))
        result = optimize(graph, options)
    except GraphStructureError as exc:
        return {
            "status": "error",
            "error": {"type": "GraphStructureError", "message": str(exc)},
        }
    except ValueError as exc:
        return {
            "status": "error",
            "error": {"type": "ValueError", "message": str(exc)},
        }
    return _result_to_dict(result)
