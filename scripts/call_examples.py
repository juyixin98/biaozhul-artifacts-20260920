"""End-to-end service call examples (standard-library HTTP only).

Assumes the server is running and fixtures are loaded::

    python -m wfst_service.main                 # terminal 1
    python scripts/load_fixtures.py --base-url http://127.0.0.1:8000
    python scripts/call_examples.py             # terminal 2

The script prints one JSON object per scenario, including normal ranked
outputs, a no-path response, and a budget-exhausted failure.
"""

from __future__ import annotations

import json
import urllib.request

BASE_URL = "http://127.0.0.1:8000"

SCENARIOS = [
    {
        "name": "pipeline composition (correct_then_morph)",
        "body": {
            "corpus_id": "demo",
            "target": "correct_then_morph",
            "input": "kat",
            "k": 5,
            "budget": 50_000,
        },
    },
    {
        "name": "lexicon mapping with ambiguity",
        "body": {
            "corpus_id": "demo",
            "target": "spell_then_lexicon",
            "input": "kat",
            "k": 4,
            "budget": 50_000,
        },
    },
    {
        "name": "epsilon deletion (z -> eps, cost 1.0)",
        "body": {
            "corpus_id": "demo",
            "target": "char_correction",
            "input": "katz",
            "k": 3,
        },
    },
    {
        "name": "no path (symbol outside alphabet)",
        "body": {
            "corpus_id": "demo",
            "target": "morphology",
            "input": "!",
            "k": 3,
        },
    },
    {
        "name": "unknown target -> 404",
        "body": {
            "corpus_id": "demo",
            "target": "ghost",
            "input": "cat",
        },
        "expect_status": 404,
    },
    {
        "name": "budget exhausted -> 503 incomplete",
        "body": {
            "corpus_id": "demo",
            "target": "char_correction",
            "input": "a",
            "k": 100,
            "budget": 12,
        },
        "expect_status": 503,
    },
]


def call(body: dict) -> tuple[int, dict]:
    req = urllib.request.Request(
        f"{BASE_URL}/query",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def main() -> None:
    for scenario in SCENARIOS:
        status, payload = call(scenario["body"])
        print("=" * 72)
        print(f"SCENARIO: {scenario['name']}")
        print(f"HTTP {status}")
        if "outputs" in payload:
            compact = {
                "run_id": payload.get("run_id"),
                "status": payload.get("status"),
                "complete": payload.get("complete"),
                "outputs": payload.get("outputs"),
                "decision": payload.get("decision"),
            }
            print(json.dumps(compact, ensure_ascii=False, indent=2))
        else:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        expected = scenario.get("expect_status", 200)
        assert status == expected, (
            f"expected HTTP {expected}, got {status}"
        )
    print("=" * 72)
    print("all scenarios behaved as documented")


if __name__ == "__main__":
    main()
