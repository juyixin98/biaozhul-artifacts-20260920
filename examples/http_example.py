"""Example: drive the HTTP service using only the Python standard library.

Start the server first:

    uvicorn hvpsvc.main:app --port 8000

then run:  python examples/http_example.py
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import os

BASE = os.environ.get("HVP_BASE_URL", "http://127.0.0.1:8000")


def call(method: str, path: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())


def main() -> None:
    spec_path = Path(__file__).resolve().parent / "quadratic.json"
    spec = json.loads(spec_path.read_text())
    created = call("POST", "/functions", spec)
    sid = created["state_id"]
    print("created state:", sid)

    call("POST", f"/functions/{sid}/point",
         {"point": {"x": [0.3, -0.5, 0.8]}})

    grad = call("GET", f"/functions/{sid}/gradient")
    print("gradient:", grad["gradient"])

    hvp_body = call("POST", f"/functions/{sid}/hvp",
                    {"vector": {"x": [1.0, -2.0, 3.0]}})
    print("H @ v  :", hvp_body["hvp"], " run:", hvp_body["run_id"])

    verified = call("POST", f"/functions/{sid}/verify",
                    {"vector": {"x": [1.0, -2.0, 3.0]}, "tolerance": 1e-8})
    print("verify passed:", verified["passed"])
    for check in verified["checks"]:
        print(f"  - {check['name']}: "
              f"{'PASS' if check['passed'] else 'FAIL'}"
              f"{' [skipped]' if check.get('skipped') else ''} "
              f"({check['reason']})")


if __name__ == "__main__":
    main()
