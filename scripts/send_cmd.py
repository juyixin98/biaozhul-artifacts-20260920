#!/usr/bin/env python3
"""Send a real signed command, query status/events, or reload the registry.

Examples (from repo root, after `source setup_env.sh`):

  python3 scripts/send_cmd.py command alpha tester_alpha dock 1 \
      '{"pose": [1.0, 2.0, 0.0]}'
  python3 scripts/send_cmd.py status alpha tester_alpha
  python3 scripts/send_cmd.py events alpha tester_alpha
  python3 scripts/send_cmd.py stream alpha tester_alpha
  python3 scripts/send_cmd.py reload
"""

from __future__ import annotations

import json
import os
import sys
import time

import httpx

from p48_gateway.client import send_command, signed_request
from p48_gateway.registry import load_registry

PORT = os.environ.get("P48_PORT", "8088")
REGISTRY_PATH = os.environ.get("P48_REGISTRY", "config/registry.example.json")
BASE_URL = f"http://127.0.0.1:{PORT}"


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    action = sys.argv[1]
    registry = load_registry(REGISTRY_PATH)

    with httpx.Client(base_url=BASE_URL, timeout=10.0, trust_env=False) as client:
        if action == "command":
            # command <robot> <tester> <target> <seq> <command_json> [ttl]
            if len(sys.argv) < 7:
                print("usage: command <robot> <tester> <target> <seq> "
                      "'<json command>' [ttl_seconds]")
                return 2
            robot, tester, target, seq, command_json = sys.argv[2:7]
            ttl = float(sys.argv[7]) if len(sys.argv) > 7 else None
            if tester not in registry.testers:
                print(f"unknown tester {tester!r}")
                return 2
            key = registry.testers[tester].key
            resp = send_command(
                client,
                BASE_URL,
                robot_id=robot,
                tester=tester,
                key=key,
                target=target,
                seq=int(seq),
                command=json.loads(command_json),
                ttl_seconds=ttl,
            )
        elif action in {"status", "events", "stream"}:
            if len(sys.argv) < 4:
                print(f"usage: {action} <robot> <tester>")
                return 2
            robot, tester = sys.argv[2], sys.argv[3]
            if tester not in registry.testers:
                print(f"unknown tester {tester!r}")
                return 2
            key = registry.testers[tester].key
            suffix = "/events/stream" if action == "stream" else (
                "/events" if action == "events" else "/status"
            )
            path = f"/robots/{robot}{suffix}"
            if action == "stream":
                headers = {}
                from p48_gateway.client import auth_headers

                headers = auth_headers(
                    method="GET", path=path, tester=tester, key=key, body=None
                )
                with client.stream("GET", path, headers=headers) as stream:
                    deadline = time.time() + 15
                    for line in stream.iter_lines():
                        if line:
                            print(line)
                        if time.time() > deadline:
                            break
                return 0
            resp = signed_request(
                client, "GET", BASE_URL + path, tester=tester, key=key
            )
        elif action == "reload":
            resp = client.post(
                "/admin/registry/reload",
                headers={"X-Admin-Key": registry.admin_key},
            )
        else:
            print(f"unknown action {action!r}")
            return 2

    print(f"HTTP {resp.status_code}")
    try:
        print(json.dumps(resp.json(), indent=2, ensure_ascii=False))
    except json.JSONDecodeError:
        print(resp.text)
    return 0 if resp.is_success else 1


if __name__ == "__main__":
    sys.exit(main())
