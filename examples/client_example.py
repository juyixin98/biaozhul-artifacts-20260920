"""Minimal client example for the krylov-expv service.

Usage:
    uvicorn krylov_expv.service.app:app --port 8000   # in another shell
    python3 examples/client_example.py

Loads the jordan4 fixture, posts it to /v1/expv, and prints the result and
the error evidence.  Also demonstrates one abnormal call (out-of-bounds
index) so the failure envelope is visible.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx

BASE = os.environ.get("KRYLOV_EXPV_URL", "http://127.0.0.1:8000")
FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "matrices" / "jordan4.json"


def main() -> None:
    fixture = json.loads(FIXTURE.read_text())
    payload = {
        "request_id": "example-jordan4-001",
        "matrix": {"n": fixture["n"], "row": fixture["row"], "col": fixture["col"], "data": fixture["data"]},
        "vector": fixture["vector"],
        "t": fixture["t"],
        "tol": fixture["tol"],
    }
    with httpx.Client(base_url=BASE, timeout=30.0) as client:
        print("== normal call: exp(t*J) @ v for the 4x4 Jordan block ==")
        r = client.post("/v1/expv", json=payload)
        r.raise_for_status()
        body = r.json()
        print(f"request_id           : {body['request_id']}")
        print(f"status               : {body['status']}")
        print(f"w                    : {body['w']}")
        print(f"total error estimate : {body['total_error_estimate']:.3e}")
        print(f"max subspace residual: {body['max_subspace_residual']:.3e}")
        print(f"steps                : {body['num_steps']}")
        print(f"versions             : {body['versions']}")

        print("\n== abnormal call: COO index out of bounds ==")
        bad = dict(payload)
        bad["request_id"] = "example-bad-002"
        bad["matrix"] = {**payload["matrix"], "row": [99] + payload["matrix"]["row"][1:]}
        r = client.post("/v1/expv", json=bad)
        print(f"HTTP {r.status_code}: {r.json()}")


if __name__ == "__main__":
    main()
