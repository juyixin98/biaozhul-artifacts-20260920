"""Minimal client example: POST a synthetic fixture to a locally running service.

Usage:
    PYTHONPATH=src uvicorn krylov_expm.service:app --port 8000   # terminal 1
    python examples/call_service.py                              # terminal 2
"""
import json
from pathlib import Path

import httpx
import numpy as np

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
BASE_URL = "http://127.0.0.1:8000"


def main():
    data = np.load(FIXTURES / "grcar_nonnormal.npz")
    body = {
        "matrix": {
            "shape": [int(x) for x in data["shape"]],
            "row": data["row"].tolist(),
            "col": data["col"].tolist(),
            "data": data["data"].tolist(),
        },
        "vector": data["vector"].tolist(),
        "t": 1.3,
        "tol": 1e-9,
    }
    headers = {"X-Request-ID": "example-call-0001"}
    with httpx.Client(base_url=BASE_URL, headers=headers, timeout=30.0) as client:
        print("GET /healthz:", client.get("/healthz").json())
        response = client.post("/v1/expmv", json=body)
    result = response.json()
    print("HTTP", response.status_code)
    print("request_id:", result["request_id"])
    print("status:", result["status"])
    print("vector_norm:", result.get("vector_norm"))
    evidence = result.get("evidence") or {}
    print("segments:", evidence.get("planned_segments"))
    print("cumulative_error_estimate:", evidence.get("cumulative_error_estimate"))
    print("total_matvec_count:", evidence.get("total_matvec_count"))
    if result.get("failure"):
        print("failure:", json.dumps(result["failure"], indent=2))


if __name__ == "__main__":
    main()
