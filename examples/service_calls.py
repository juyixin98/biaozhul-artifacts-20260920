#!/usr/bin/env python3
"""End-to-end service call examples against a locally running API.

Start the server first (terminal 1):
    PYTHONPATH=src python -m uvicorn ipwate.api:app --port 8000

Then run (terminal 2):
    PYTHONPATH=src python examples/service_calls.py

Everything uses local synthetic data; no external accounts.
"""

from __future__ import annotations

import json
import os

import httpx

from ipwate.synthetic import generate_synthetic

BASE_URL = os.environ.get("IPW_BASE_URL", "http://127.0.0.1:8000")
TIMEOUT = 30.0


def _print(title: str, payload: dict) -> None:
    print(f"\n===== {title} =====")
    print(json.dumps(payload, indent=2, allow_nan=False)[:1800])


def main() -> int:
    client = httpx.Client(base_url=BASE_URL, timeout=TIMEOUT)

    health = client.get("/healthz")
    _print("GET /healthz", health.json())

    # 1) Normal run: good overlap, custom request id for tracing.
    data = generate_synthetic(n=2000, scenario="good_overlap", seed=2024)
    ok = client.post(
        "/api/v1/ipw/estimate",
        json={
            "x": data.x.tolist(),
            "a": data.a.tolist(),
            "y": data.y.tolist(),
            "request_id": "example-good",
        },
        headers={"x-request-id": "trace-001"},
    )
    body = ok.json()
    _print(
        "POST good_overlap (verdict + estimate + weights)",
        {
            "http_status": ok.status_code,
            "x-request-id": ok.headers.get("x-request-id"),
            "verdict": body["result"]["verdict"],
            "estimate": body["result"]["estimate"],
            "weights_ess": {
                "treated_ess_fraction": body["result"]["weights"]["treated_ess_fraction"],
                "untreated_ess_fraction": body["result"]["weights"]["untreated_ess_fraction"],
            },
            "findings": [f["code"] for f in body["result"]["findings"]],
        },
    )

    # 2) Positivity violation -> structured 422 with a failure CATEGORY.
    bad = generate_synthetic(n=2000, scenario="no_overlap", seed=9)
    rejected = client.post(
        "/api/v1/ipw/estimate",
        json={"x": bad.x.tolist(), "a": bad.a.tolist(), "y": bad.y.tolist()},
    )
    _print(
        "POST no_overlap (expected 422 positivity_violation)",
        {"http_status": rejected.status_code, **rejected.json()},
    )

    # 3) Poor overlap with the FIXED clipping profile declared.
    poor = generate_synthetic(n=3000, scenario="poor_overlap", seed=11)
    clipped = client.post(
        "/api/v1/ipw/estimate",
        json={
            "x": poor.x.tolist(),
            "a": poor.a.tolist(),
            "y": poor.y.tolist(),
            "clipping_enabled": True,
            "estimand": "ate",
        },
    )
    cbody = clipped.json()["result"]
    _print(
        "POST poor_overlap + fixed clipping (trimmed estimand disclosed)",
        {
            "http_status": clipped.status_code,
            "verdict": cbody["verdict"],
            "clipping": {
                "enabled": cbody["contract"]["clipping_enabled"],
                "profile": cbody["contract"]["clipping_profile"],
                "note": cbody["contract"]["estimand_note"],
            },
            "n_clipped": cbody["overlap"]["n_clipped"],
            "findings": [f["code"] for f in cbody["findings"]],
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
