#!/usr/bin/env python3
"""Command-line entry point: solve a problem JSON file or replay a schedule.

Examples
--------
Solve (writes evidence to the configured SQLite DB)::

    python -m scripts.run_cli solve tests/fixtures/robot_carry.json

Replay an explicit schedule (independent verification, no search)::

    python -m scripts.run_cli replay tests/fixtures/robot_carry.json \
        --at carry_a:0:3 --at carry_b:3:5
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Allow running both as ``python -m scripts.run_cli`` and as a direct file.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tplan.config import SETTINGS  # noqa: E402
from tplan.model import Problem  # noqa: E402
from tplan.service import PlanningService  # noqa: E402
from tplan.store import EvidenceStore  # noqa: E402


def _load(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Finite-action temporal planner")
    sub = parser.add_subparsers(dest="command", required=True)

    solve_p = sub.add_parser("solve", help="solve a problem JSON file")
    solve_p.add_argument("problem", help="path to problem JSON")
    solve_p.add_argument("--node-budget", type=int, default=None)
    solve_p.add_argument("--time-budget", type=float, default=None)
    solve_p.add_argument("--show-timeline", action="store_true")
    solve_p.add_argument("--show-trace", action="store_true")

    replay_p = sub.add_parser("replay", help="independently replay a schedule")
    replay_p.add_argument("problem")
    replay_p.add_argument(
        "--at",
        action="append",
        default=[],
        metavar="ACTION:START:END",
        help="scheduled item, repeatable",
    )

    args = parser.parse_args(argv)
    raw = _load(args.problem)

    store = EvidenceStore(SETTINGS.db_path)
    service = PlanningService(SETTINGS, store)
    try:
        if args.command == "solve":
            resp = service.solve(
                raw,
                node_budget=args.node_budget,
                time_budget_seconds=args.time_budget,
            )
            out = {
                "run_id": resp.run_id,
                "input_sha256": resp.input_sha256,
                "status": resp.status,
                "failure_code": resp.failure_code,
                "optimal": resp.optimal,
                "best_cost": resp.best_cost,
                "goal_time": resp.goal_time,
                "nodes_expanded": resp.nodes_expanded,
                "node_budget": resp.node_budget,
                "elapsed_seconds": resp.elapsed_seconds,
                "message": resp.message,
                "schedule": resp.schedule,
            }
            if args.show_timeline:
                out["timeline"] = resp.timeline
            if args.show_trace:
                out["trace"] = resp.trace
            print(json.dumps(out, indent=2, ensure_ascii=False))
            return 0 if resp.status in ("optimal", "feasible_not_proven_optimal") else 2

        items = []
        for token in args.at:
            aid, start, end = token.split(":")
            items.append({"action_id": aid, "start": int(start), "end": int(end)})
        result = service.replay(raw, items)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0 if result["ok"] else 3
    finally:
        service.close()


if __name__ == "__main__":
    raise SystemExit(main())
