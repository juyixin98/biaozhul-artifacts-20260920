#!/usr/bin/env python3
"""Record real planning runs to docs/results/.

Requires a running server (see scripts/serve_and_record.sh):
    PLANNER_BASE=http://127.0.0.1:8000 python scripts/record_runs.py
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "docs" / "results"
BASE = os.environ.get("PLANNER_BASE", "http://127.0.0.1:8000")


def post(path: str, payload: dict | bytes):
    data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    request = urllib.request.Request(
        BASE + path, data=data,
        headers={"content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        return exc.code, json.load(exc)


def get(path: str):
    with urllib.request.urlopen(BASE + path) as response:
        return json.load(response)


def save(name: str, status: int, body: dict) -> dict:
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / f"{name}.json").write_text(
        json.dumps({"http_status": status, "response": body},
                   indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"{name:32s} HTTP {status}")
    return body


def load(name: str):
    return json.loads((ROOT / "fixtures" / name).read_text())


def main() -> None:
    dom = load("domain_resource_ops.json")
    route = load("domain_route_graph.json")

    status, body = post("/plan", {
        "domain": dom, "problem": load("problem_solvable.json"),
        "options": {"algorithm": "ucs", "heuristic": "zero"}})
    solved = save("01_solvable_resource_ops", status, body)

    save("02_unsolvable_missing_pred", *post("/plan", {
        "domain": dom, "problem": load("problem_unsolvable_missing.json"),
        "options": {"algorithm": "astar", "heuristic": "h_max"}}))
    save("03_unsolvable_sealed_dock", *post("/plan", {
        "domain": dom, "problem": load("problem_unsolvable_sealed.json")}))
    save("04_cycles_power", *post("/plan", {
        "domain": dom, "problem": load("problem_cycles.json")}))
    save("05a_cost_paths_bfs", *post("/plan", {
        "domain": route, "problem": load("problem_route_cost.json"),
        "options": {"algorithm": "bfs", "heuristic": "zero"}}))
    save("05b_cost_paths_ucs", *post("/plan", {
        "domain": route, "problem": load("problem_route_cost.json"),
        "options": {"algorithm": "ucs", "heuristic": "zero"}}))
    save("05c_cost_paths_astar_hadd", *post("/plan", {
        "domain": route, "problem": load("problem_route_cost.json"),
        "options": {"algorithm": "astar", "heuristic": "h_add"}}))
    unknown = save("06_unknown_node_limit", *post("/plan", {
        "domain": dom, "problem": load("problem_unsolvable_missing.json"),
        "options": {"max_expansions": 50}}))
    save("07_unknown_depth_limit", *post("/plan", {
        "domain": dom, "problem": load("problem_solvable.json"),
        "options": {"algorithm": "bfs", "max_depth": 2}}))

    bad_var = json.loads(json.dumps(dom))
    bad_var["actions"][0]["add"].append("ghost(?zz)")
    save("08_input_error_unbound_var", *post("/plan", {
        "domain": bad_var, "problem": load("problem_solvable.json")}))

    conflict_domain = {"name": "d", "actions": [{
        "name": "a", "parameters": ["?x"],
        "preconditions": {"pos": ["p(?x)"], "neg": []},
        "add": ["q(?x)"], "delete": ["q(?x)"], "cost": 1}]}
    save("09_input_error_add_del_conflict", *post("/plan", {
        "domain": conflict_domain,
        "problem": {"name": "p", "objects": ["o1"], "init": ["p(o1)"],
                    "goal": {"pos": ["q(o1)"], "neg": []}}}))
    save("10_resource_exhausted_grounding", *post("/plan", {
        "domain": route, "problem": load("problem_route_cost.json"),
        "options": {"ground_actions_limit": 5}}))
    save("11_malformed_json", *post("/plan", b"{not json"))

    run_id = solved["run_id"]
    (RESULTS / "12_solvable_steps.json").write_text(
        json.dumps(get(f"/runs/{run_id}/steps"), indent=2, ensure_ascii=False))
    (RESULTS / "13_solvable_trace.json").write_text(
        json.dumps(get(f"/runs/{run_id}/trace"), indent=2, ensure_ascii=False))
    (RESULTS / "14_run_record.json").write_text(
        json.dumps(get(f"/runs/{unknown['run_id']}"), indent=2, ensure_ascii=False))
    status, replay = post(f"/runs/{run_id}/replay", b"")
    save("15_replay", status, replay)
    (RESULTS / "16_runs_list.json").write_text(
        json.dumps(get("/runs?limit=5"), indent=2, ensure_ascii=False))
    print("recorded to", RESULTS)


if __name__ == "__main__":
    main()
