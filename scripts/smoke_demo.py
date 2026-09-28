#!/usr/bin/env python3
"""End-to-end smoke demo against the real ASGI app (in-process TestClient).

Run from the repository root::

    python3 scripts/smoke_demo.py

It creates a throwaway SQLite database under ``data/demo/``, posts a ruleset
that exhibits both pairwise overlaps and an unreachable rule, lexs sample
text, and replays the structured run log.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402

RULESET = {
    "name": "demo",
    "rules": [
        {"name": "A", "pattern": "a[bc]+d", "priority": 0},
        {"name": "B", "pattern": "[ab]c*d", "priority": 1},
        {"name": "C", "pattern": "[a-z]+", "priority": 2},
        {"name": "KW", "pattern": "if", "priority": 3},
        {"name": "WS", "pattern": "[ \\t\\n]+", "priority": 4},
    ],
}


def main() -> None:
    settings = Settings(db_path="data/demo/lexer.db")
    client = TestClient(create_app(settings))

    print("== POST /api/rulesets ==")
    created = client.post("/api/rulesets", json=RULESET)
    print("status:", created.status_code)
    body = created.json()
    print(json.dumps(body["diagnostics"], indent=2, ensure_ascii=False))
    ruleset_id = body["ruleset_id"]
    compile_run = body["run_id"]

    print("== POST /api/rulesets/{id}/lex ==")
    # "if" belongs to both C and KW; the diagnostics flag KW as unreachable,
    # so the lexer (longest match, then priority) hands "if" to C.
    lexed = client.post(
        f"/api/rulesets/{ruleset_id}/lex", json={"text": "acd if"}
    )
    print("status:", lexed.status_code)
    print(json.dumps(lexed.json(), indent=2, ensure_ascii=False))
    lex_run = lexed.json()["run_id"]

    print("== GET /api/runs/{compile_run} (replay) ==")
    replayed = client.get(f"/api/runs/{compile_run}")
    print("status:", replayed.status_code)
    for entry in replayed.json()["entries"]:
        print(f"  [{entry['stage']}] {json.dumps(entry['detail'], ensure_ascii=False)}")

    print("== bad pattern (INPUT_ERROR), replayed from its run id ==")
    bad = client.post(
        "/api/rulesets", json={"name": "bad", "rules": [{"name": "X", "pattern": "a*"}]}
    )
    print("status:", bad.status_code, json.dumps(bad.json()["error"], ensure_ascii=False))
    bad_run = client.get(f"/api/runs/{bad.json()['run_id']}").json()
    print("  log:", json.dumps(bad_run["entries"][0]["detail"], ensure_ascii=False))
    print("  lex run id for reference:", lex_run)


if __name__ == "__main__":
    main()
