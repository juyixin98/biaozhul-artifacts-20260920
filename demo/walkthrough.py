#!/usr/bin/env python3
"""End-to-end demonstration script (stdlib only).

Run the API first (docker compose up, or uvicorn locally), then:

    python demo/walkthrough.py --base-url http://localhost:8000

It publishes the demo templates, starts an expense instance, demonstrates
任签 (any-one) approval, walks the high-amount 全签 (countersign) path,
shows idempotent replay and prints current position + history.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
import uuid


def call(method: str, base: str, path: str, body=None, headers=None):
    data = None
    req_headers = {"Content-Type": "application/json"}
    if headers:
        req_headers.update(headers)
    if body is not None:
        data = json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers=req_headers)
    try:
        with urllib.request.urlopen(req) as resp:
            payload = json.loads(resp.read().decode() or "null")
            return resp.status, payload
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def show(title, status, payload):
    print(f"\n=== {title} (HTTP {status}) ===")
    print(json.dumps(payload, ensure_ascii=False, indent=2)[:2000])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8000")
    args = parser.parse_args()
    base = args.base_url.rstrip("/")

    status, payload = call("POST", base, "/demo/setup")
    show("seed demo templates", status, payload)

    # Low-amount any-sign flow
    req_id = str(uuid.uuid4())
    status, inst = call(
        "POST", base, "/instances",
        {
            "template_code": "expense-demo",
            "business_key": f"EXP-{uuid.uuid4().hex[:8]}",
            "variables": {"amount": 500, "reason": "books"},
        },
        headers={"X-Request-Id": req_id, "X-User-Id": "alice"},
    )
    show("start instance (amount=500)", status, inst)
    instance_id = inst["id"]
    version = inst["template_version"]

    # Idempotent replay of the start
    status, replay = call(
        "POST", base, "/instances",
        {
            "template_code": "expense-demo",
            "business_key": "SHOULD-NOT-MATTER",
            "variables": {},
        },
        headers={"X-Request-Id": req_id, "X-User-Id": "alice"},
    )
    show("replay same request id -> original instance", status,
         {"id": replay.get("id"), "same": replay.get("id") == instance_id})

    # Any-sign: manager1 approves -> manager2's task is closed automatically
    status, after = call(
        "POST", base, f"/instances/{instance_id}/decision",
        {"action": "approve", "expected_version": version,
         "comment": "ok"},
        headers={"X-Request-Id": str(uuid.uuid4()), "X-User-Id": "manager1"},
    )
    show("manager1 approves (任签)", status,
         {"status": after["status"], "current": after["current_node_id"]})

    status, detail = call("GET", base, f"/instances/{instance_id}")
    show("final position + history", status,
         {"status": detail["status"],
          "history": [e["event_type"] for e in detail["history"]]})

    # High-amount countersign flow
    req_id = str(uuid.uuid4())
    status, big = call(
        "POST", base, "/instances",
        {
            "template_code": "expense-demo",
            "business_key": f"EXP-{uuid.uuid4().hex[:8]}",
            "variables": {"amount": 50000},
        },
        headers={"X-Request-Id": req_id, "X-User-Id": "bob"},
    )
    big_id = big["id"]
    status, step1 = call(
        "POST", base, f"/instances/{big_id}/decision",
        {"action": "approve", "expected_version": version},
        headers={"X-Request-Id": str(uuid.uuid4()), "X-User-Id": "manager1"},
    )
    status, f1 = call(
        "POST", base, f"/instances/{big_id}/decision",
        {"action": "approve", "expected_version": version},
        headers={"X-Request-Id": str(uuid.uuid4()), "X-User-Id": "finance1"},
    )
    show("finance1 approved; still waiting finance2 (全签)", status,
         {"status": f1["status"], "current": f1["current_node_id"],
          "pending": [t["assignee"] for t in f1["pending_tasks"]]})
    status, f2 = call(
        "POST", base, f"/instances/{big_id}/decision",
        {"action": "approve", "expected_version": version},
        headers={"X-Request-Id": str(uuid.uuid4()), "X-User-Id": "finance2"},
    )
    show("finance2 approved -> finished", f2["status"],
         {"status": f2["status"], "current": f2["current_node_id"]})
    return 0


if __name__ == "__main__":
    sys.exit(main())
