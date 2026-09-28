#!/usr/bin/env python3
"""Example: call the isolation service over HTTP.

Usage::

    # terminal 1
    python -m root_isolator.service
    # terminal 2
    python examples/call_api.py

The script also works without a network if the service is unavailable: it then
falls back to the in-process application pipeline so the examples always run.

Cases demonstrated:
  1. a repeated root           (x-1)^2 (x+2)
  2. close roots               (x-1)(x-(1+1/2^50))
  3. no real roots             x^2 + 1
  4. high-dynamic coefficients 10^40 x^2 - 1
  5. the zero polynomial       (special-cased)
  6. a rejected non-exact float coefficient
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

# Allow running directly from the examples/ directory without installation.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

URL = "http://127.0.0.1:8000/api/v1/isolate"

CASES: list[tuple[str, dict]] = [
    (
        "repeated root: (x-1)^2 (x+2) = x^3 - 3x + 2",
        {"coefficients": [1, 0, -3, 2]},
    ),
    (
        "close roots: (x-1)(x-(1+1/2^50)), exact sparse form",
        {
            "sparse": {
                "0": f"{2**50 + 1}/{2**50}",        # 1 + 1/2^50
                "1": f"-{2**51 + 1}/{2**50}",       # -(2 + 1/2^50)
                "2": "1",
            }
        },
    ),
    ("no real roots: x^2 + 1", {"coefficients": [1, 0, 1]}),
    ("high dynamic range: 10^40 x^2 - 1", {"coefficients": [f"{10**40}", 0, -1]}),
    ("zero polynomial", {"coefficients": [0, 0, 0]}),
    ("exact fraction string: x^2 - 1/4", {"coefficients": ["1", "0", "-1/4"]}),
]


def post_over_http(payload: dict) -> tuple[int, dict]:
    request = urllib.request.Request(
        URL,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "X-Request-ID": "example-call"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def post_in_process(payload: dict) -> tuple[int, dict]:
    from config.settings import BudgetConfig, NumericConfig, LogConfig, ServiceConfig
    from root_isolator.errors import HTTP_STATUS_BY_CATEGORY, IsolationError
    from root_isolator.service.application import isolate_from_payload
    from root_isolator.service.diagnostics import Diagnostics, configure_logging
    from root_isolator.service.serialization import serialize_error, serialize_verdict

    config = ServiceConfig(budget=BudgetConfig(), numeric=NumericConfig(), log=LogConfig())
    logger = configure_logging(config.log)
    diagnostics = Diagnostics(logger, config.log, "example-call")
    try:
        outcome = isolate_from_payload(
            payload,
            budget=config.budget,
            numeric=config.numeric,
            diagnostics=diagnostics,
        )
    except IsolationError as exc:
        return HTTP_STATUS_BY_CATEGORY.get(exc.category, 500), serialize_error(
            request_id="example-call",
            category=exc.category.value,
            message=exc.message,
            state=exc.state,
        )
    return 200, serialize_verdict(outcome.verdict, request_id="example-call", meta=outcome.meta)


def summarise(status: int, body: dict) -> str:
    if "error" in body:
        return f"HTTP {status} [{body['error']['category']}] {body['error']['message']}"
    counts = body["root_counts"]
    intervals = []
    for iv in body["intervals"]:
        intervals.append(
            f"    ({iv['left']['decimal_display']}, {iv['right']['decimal_display']}]"
            f" multiplicity={iv['multiplicity']}"
        )
    lines = [
        f"HTTP {status} verdict={body['verdict']} kind={body['result_kind']}",
        f"    distinct real roots={counts['distinct_real_roots']}, "
        f"with multiplicity={counts['real_roots_with_multiplicity']}, "
        f"complex={counts['complex_roots_with_multiplicity']}",
    ]
    lines.extend(intervals)
    if body["evidence"]["reasons"]:
        lines.append(f"    evidence: {body['evidence']['reasons'][0]}")
    return "\n".join(lines)


def main() -> int:
    # Probe the server once; fall back to in-process if it is not running.
    try:
        post_over_http(CASES[0][1])
        transport = post_over_http
        print("(using live HTTP service)\n")
    except (urllib.error.URLError, ConnectionError):
        transport = post_in_process
        print("(service not reachable; using in-process pipeline)\n")

    for title, payload in CASES:
        print(f"== {title}")
        status, body = transport(payload)
        print(summarise(status, body))
        print()

    print("== rejected request: binary float coefficient")
    status, body = transport({"coefficients": [0.1, 0, -1]})
    print(summarise(status, body))
    return 0


if __name__ == "__main__":
    sys.exit(main())
