"""Command-line replay/diagnosis tool.

Examples
--------
Show the last run::

    python -m rationalsvc.replay --last

Replay a specific run (recompute from its logged exact input)::

    python -m rationalsvc.replay run-20260927T....-ab12cd

Replay with a different digit budget::

    python -m rationalsvc.replay --last --digit-budget 50

List recent runs::

    python -m rationalsvc.replay --list
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import numeric_input, runner
from .errors import ServiceError

RUN_LOG_PATH = os.environ.get("RATIONALSVC_RUN_LOG", "run_log.jsonl")


def _load_events(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    events: list[dict] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return events


def _runs(events: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for ev in events:
        grouped.setdefault(ev["run_id"], []).append(ev)
    return grouped


def _replay(run_id: str, events: list[dict], digit_budget: int | None) -> dict:
    started = next((e for e in events if e["event"] == "started"), None)
    if started is None:
        raise SystemExit(f"run {run_id!r} has no 'started' event; cannot replay")
    payload = {"A": started["A"], "b": _rows_to_b(started),
               "want": started.get("want", "both")}
    if digit_budget is not None:
        payload["digit_budget"] = digit_budget
    else:
        payload["digit_budget"] = started.get("digit_budget", 4096)

    parsed = numeric_input.parse_request(payload)
    logger = runner.RunLogger(None)  # replay does not append to the log
    return runner.solve_system(parsed, logger, run_id=f"replay-{run_id}")


def _rows_to_b(started: dict):
    # Logged b is row-major with possibly multiple rhs; restore that shape.
    b = started["b"]
    if started.get("rhs_count", 1) == 1:
        return [row[0] for row in b]
    return b


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Replay exact matrix service runs")
    ap.add_argument("run_id", nargs="?", help="run id to replay")
    ap.add_argument("--log", default=RUN_LOG_PATH, help="JSONL run log path")
    ap.add_argument("--last", action="store_true", help="replay the most recent run")
    ap.add_argument("--list", action="store_true", help="list recent runs")
    ap.add_argument("--digit-budget", type=int, default=None,
                    help="override the digit budget for the replay")
    ap.add_argument("--no-float-diagnosis", action="store_true",
                    help="skip the (advisory) floating-point diagnosis")
    args = ap.parse_args(argv)

    events = _load_events(args.log)
    grouped = _runs(events)

    if args.list or (args.run_id is None and not args.last):
        for rid, evs in list(grouped.items())[-20:]:
            kinds = [e["event"] for e in evs]
            verdict = "completed" if "completed" in kinds else (
                "budget_exhausted" if "budget_exhausted" in kinds else "error")
            print(f"{rid}  events={len(evs):2d}  {verdict}")
        return 0

    run_id = args.run_id
    if args.last:
        if not grouped:
            print("no runs found", file=sys.stderr)
            return 1
        run_id = list(grouped)[-1]
    if run_id not in grouped:
        print(f"unknown run id: {run_id!r}", file=sys.stderr)
        return 1

    try:
        result = _replay(run_id, grouped[run_id], args.digit_budget)
    except ServiceError as exc:
        print(json.dumps(exc.to_dict(), indent=2, ensure_ascii=False))
        return 2
    if args.no_float_diagnosis:
        result.pop("float_diagnosis", None)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
