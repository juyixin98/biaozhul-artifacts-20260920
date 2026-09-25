#!/usr/bin/env python3
"""Sample client: POST examples/split_request.json to the local service.

Usage:
    # terminal 1
    .venv/bin/python -m group_split.service --port 8080
    # terminal 2
    .venv/bin/python examples/client.py [--port 8080]
"""

from __future__ import annotations

import argparse
import json
import urllib.request
from pathlib import Path

REQUEST_FILE = Path(__file__).with_name("split_request.json")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    payload = REQUEST_FILE.read_bytes()
    req = urllib.request.Request(
        f"http://{args.host}:{args.port}/split",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        body = json.loads(resp.read())
    print(json.dumps(body, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
