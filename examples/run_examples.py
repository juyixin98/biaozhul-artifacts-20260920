#!/usr/bin/env python3
"""Send examples/payloads.jsonl against a running server and verify answers.

Each line is a valid /decode request; keys prefixed with ``_`` are local
assertions instead of being sent:

* ``_expect``: signal name -> expected raw/value, plus ``mux`` and
  ``message_name``; ``_absent`` lists signals that must NOT be decoded
  (inactive multiplex branches).
* ``_expect_error``: an HTTP 4xx must come back with that error code.

Usage::

    python examples/run_examples.py                     # default base URL
    python examples/run_examples.py --base http://127.0.0.1:8000
"""

from __future__ import annotations

import argparse
import json
import math
import pathlib
import sys

import httpx

HERE = pathlib.Path(__file__).resolve().parent


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    base = args.base.rstrip("/")

    with open(HERE / "demo.dbc", encoding="utf-8") as fh:
        dbc_text = fh.read()

    # trust_env=False: this always talks to a local loopback server, so do
    # not pick up HTTP(S)_PROXY / ALL_PROXY from the surrounding shell.
    with httpx.Client(base_url=base, timeout=10, trust_env=False) as client:
        uploaded = client.post("/dbc", json={"dbc": dbc_text})
        uploaded.raise_for_status()
        body = uploaded.json()
        version_id = body["version_id"]
        print(f"[dbc] uploaded version {version_id[:16]}... "
              f"({body['message_count']} messages, created={body['created']})")

        failures = 0
        lines = [
            line for line in (HERE / "payloads.jsonl")
            .read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        for number, line in enumerate(lines, 1):
            case = json.loads(line)
            expect = case.pop("_expect", None)
            expect_error = case.pop("_expect_error", None)
            expect_status = case.pop("_expect_http_status", None)
            response = client.post("/decode", json=case)

            if expect_status is not None:
                if response.status_code != expect_status:
                    print(f"[case {number}] FAIL expected HTTP "
                          f"{expect_status}, got {response.status_code}: "
                          f"{response.text}")
                    failures += 1
                else:
                    print(f"[case {number}] OK rejected (HTTP "
                          f"{expect_status})")
                continue

            if expect_error is not None:
                if response.status_code < 400:
                    print(f"[case {number}] FAIL expected error "
                          f"{expect_error['code']}, got 200: {response.text}")
                    failures += 1
                    continue
                code = response.json()["error"]["code"]
                if code != expect_error["code"]:
                    print(f"[case {number}] FAIL expected error code "
                          f"{expect_error['code']}, got {code}")
                    failures += 1
                    continue
                print(f"[case {number}] OK rejected ({code})")
                continue

            if response.status_code >= 400:
                print(f"[case {number}] FAIL {response.status_code}: "
                      f"{response.text}")
                failures += 1
                continue

            result = response.json()
            problems = []
            if expect.get("message_name") and \
                    result["message_name"] != expect["message_name"]:
                problems.append(
                    f"message_name {result['message_name']!r} != "
                    f"{expect['message_name']!r}"
                )
            if "mux" in expect and result["mux"] != expect["mux"]:
                problems.append(f"mux {result['mux']} != {expect['mux']}")
            by_name = {s["name"]: s for s in result["signals"]}
            absent_names = expect.pop("_absent", [])
            for sig_name, fields in expect.items():
                if sig_name in ("message_name", "mux"):
                    continue
                got = by_name.get(sig_name)
                if got is None:
                    problems.append(f"signal {sig_name!r} missing")
                    continue
                for key, wanted in fields.items():
                    actual = got.get(key)
                    if isinstance(wanted, float):
                        ok = isinstance(actual, (int, float)) and math.isclose(
                            actual, wanted, abs_tol=1e-9
                        )
                    else:
                        ok = actual == wanted
                    if not ok:
                        problems.append(
                            f"{sig_name}.{key} = {actual!r} != {wanted!r}"
                        )
            for absent in absent_names:
                if absent in by_name:
                    problems.append(f"{absent!r} should be absent for branch")

            if problems:
                failures += 1
                print(f"[case {number}] FAIL frame 0x{case['frame_id']:X}:")
                for problem in problems:
                    print(f"    - {problem}")
            else:
                raws = ", ".join(
                    f"{s['name']}={s['raw']} -> {s['value']}{s['unit']}"
                    for s in result["signals"]
                )
                print(f"[case {number}] OK frame 0x{case['frame_id']:X}: {raws}")

    print(f"\n{len(lines) - failures}/{len(lines)} cases passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
