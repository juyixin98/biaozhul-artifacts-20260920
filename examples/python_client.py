"""Minimal Python client example (stdlib only).

Run while the server is up (`python -m strips_planner.service`):

    python examples/python_client.py
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE = "http://127.0.0.1:8000"


def post(path: str, payload: dict) -> tuple[int, dict]:
    request = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode(),
        headers={"content-type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        return exc.code, json.load(exc)


def get(path: str) -> dict:
    with urllib.request.urlopen(BASE + path) as response:
        return json.load(response)


def main() -> None:
    domain = json.loads((ROOT / "fixtures/domain_route_graph.json").read_text())
    problem = json.loads((ROOT / "fixtures/problem_route_cost.json").read_text())

    status, body = post("/plan", {
        "domain": domain,
        "problem": problem,
        "options": {"algorithm": "astar", "heuristic": "h_max"},
    })
    print("POST /plan ->", status)
    print("  verdict:", body["verdict"], "| cost:", body["cost"],
          "| optimal:", body["optimal_guarantee"])
    print("  plan:", [(s["name"], s["args"]) for s in body["plan"]])
    print("  independent verification valid:",
          body["verification"]["valid"])

    run_id = body["run_id"]
    print("GET /runs ->", [r["run_id"] for r in get("/runs")["runs"]])
    print("GET trace length:", len(get(f"/runs/{run_id}/trace")["trace"]))


if __name__ == "__main__":
    main()
