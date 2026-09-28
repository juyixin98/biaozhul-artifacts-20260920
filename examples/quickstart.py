#!/usr/bin/env python3
"""End-to-end example against a locally running server.

Start the server first:
    uvicorn hvp_service.main:app --port 8000

Then run:
    python examples/quickstart.py
"""

from __future__ import annotations

import httpx

BASE = "http://127.0.0.1:8000"

# f(x) = 0.5 x^T A x + b^T x  with  A = [[2, 0.5], [0.5, 3]],  b = [1, -1]
GRAPH = {
    "nodes": [
        {"op": "input", "params": {"name": "x", "shape": [2]}},
        {"op": "const", "params": {"value": [[2.0, 0.5], [0.5, 3.0]]}},
        {"op": "reshape", "inputs": [0], "params": {"shape": [2, 1]}},
        {"op": "matmul", "inputs": [1, 2]},
        {"op": "reshape", "inputs": [3], "params": {"shape": [2]}},
        {"op": "mul", "inputs": [0, 4]},
        {"op": "sum", "inputs": [5]},
        {"op": "const", "params": {"value": 0.5}},
        {"op": "mul", "inputs": [6, 7]},
        {"op": "const", "params": {"value": [1.0, -1.0]}},
        {"op": "mul", "inputs": [0, 9]},
        {"op": "sum", "inputs": [10]},
        {"op": "add", "inputs": [8, 11]},
    ],
    "output": 12,
}


def main() -> None:
    client = httpx.Client(base_url=BASE, timeout=10.0)

    resp = client.post("/graphs", json=GRAPH)
    resp.raise_for_status()
    graph_id = resp.json()["graph_id"]
    print("created graph:", graph_id)

    # HVP at x = [1, 2] in direction v = [0.5, -1].
    # Analytic answer: H v = A v = [2*0.5 + 0.5*(-1), 0.5*0.5 + 3*(-1)] = [0.5, -2.75]
    resp = client.post(
        f"/graphs/{graph_id}/hvp",
        json={"point": [1.0, 2.0], "vector": [0.5, -1.0]},
    )
    resp.raise_for_status()
    body = resp.json()
    print("run_id:  ", body["run_id"])
    print("value:   ", body["value"])  # 0.5 * [1,2]A[1,2]^T + [1,-1].[1,2] = 8 - 1 = 7.0
    print("gradient:", body["gradient"])  # A x + b = [4.0, 5.5]
    print("hvp:     ", body["hvp"])  # [0.5, -2.75]
    print("diagnostics:", body["diagnostics"])

    # Training-state flow: pin a stored point and use optimistic versioning.
    version = client.post(f"/graphs/{graph_id}/point", json={"point": [1.0, 2.0]}).json()[
        "point_version"
    ]
    resp = client.post(
        f"/graphs/{graph_id}/hvp",
        json={"vector": [0.5, -1.0], "use_stored_point": True, "expected_version": version},
    )
    print("stored-point hvp:", resp.json()["hvp"])

    # A stale version is rejected as a state conflict.
    client.post(f"/graphs/{graph_id}/point", json={"point": [1.5, 2.0]})
    resp = client.post(
        f"/graphs/{graph_id}/hvp",
        json={"vector": [0.5, -1.0], "use_stored_point": True, "expected_version": version},
    )
    print("stale version ->", resp.status_code, resp.json()["error"]["category"])

    client.delete(f"/graphs/{graph_id}")
    print("graph deleted")


if __name__ == "__main__":
    main()
