"""Minimal Python client example for the STRIPS planning service.

Start the server first (``bash scripts/run_service.sh``), then::

    PYTHONPATH=src python3 examples/python_client.py            # default A*
    PYTHONPATH=src python3 examples/python_client.py --algorithm bfs
    PYTHONPATH=src python3 examples/python_client.py \
        --fixture fixtures/resource_ops_unsolvable.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def main() -> int:
    parser = argparse.ArgumentParser(description="Call the STRIPS planning service.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--fixture", default=str(FIXTURES / "resource_ops.json"))
    parser.add_argument("--algorithm", default="astar",
                        choices=["bfs", "ucs", "astar", "greedy"])
    parser.add_argument("--heuristic", default="hmax",
                        choices=["zero", "hmax", "goalcount"])
    parser.add_argument("--max-depth", type=int, default=None)
    args = parser.parse_args()

    problem = json.loads(Path(args.fixture).read_text(encoding="utf-8"))
    options = {"algorithm": args.algorithm, "heuristic": args.heuristic}
    if args.max_depth is not None:
        options["max_depth"] = args.max_depth

    response = httpx.post(
        f"{args.base_url}/api/v1/plans",
        json={"problem": problem, "options": options},
        timeout=60,
    )
    body = response.json()
    print(json.dumps(body, indent=2, ensure_ascii=False))

    if not body["success"]:
        category = body["error"]["category"]
        print(f"\nclassified as: {category}", file=sys.stderr)
        return 1 if category in ("INPUT_INVALID", "INVALID_PROBLEM",
                                 "COMPUTATION_FAILED") else 2
    result = body["result"]
    print(f"\nstatus={result['status']} "
          f"plan_length={result.get('plan_length')} "
          f"path_cost={result.get('path_cost')} "
          f"optimal={result.get('optimal')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
