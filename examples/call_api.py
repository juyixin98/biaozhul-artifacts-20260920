#!/usr/bin/env python3
"""Runnable walkthrough of the Summation Comparison API.

Usage:
    python3 examples/call_api.py [--base-url http://127.0.0.1:8000]

Demonstrates, with real calls:
  1. large-number cancellation across orderings;
  2. the chunked invariant (carrying compensation vs. adding local totals);
  3. the small-number accumulation (eps tail) scenario;
  4. special-value policy and signed zeros;
  5. an error envelope with its failure category;
  6. request-id propagation.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request


def _post(base_url: str, path: str, payload: dict, headers: dict | None = None):
    request = urllib.request.Request(
        base_url.rstrip("/") + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, dict(response.headers), json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), json.loads(exc.read())


def _section(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    base = args.base_url

    _section("1. Large-number cancellation: [1e16, 1 x100000, -1e16]")
    status, _, body = _post(
        base, "/api/v1/compare",
        {"values": [1e16] + [1] * 100_000 + [-1e16], "block_size": 4096,
         "orderings": ["original", "abs_ascending"]},
    )
    if status != 200:
        print(f"service not reachable / error {status}: is uvicorn running at {base}?")
        print(body)
        return 1
    print(f"reference ({body['reference']['method']}): {body['reference']['value']}")
    for ordering, entry in body["orderings"].items():
        methods = entry["methods"]
        print(
            f"  {ordering:14s} naive={methods['naive']['result']:<10} "
            f"kahan={methods['kahan']['result']:<10} pairwise={methods['pairwise']['result']}"
        )
    chunked = body["orderings"]["original"]["chunked"]
    print("  chunked on original order:")
    print(f"    naive_sharded (add local totals) abs error = "
          f"{chunked['naive_sharded']['abs_error']}")
    print(f"    kahan_merged (carry dd compensation) abs error = "
          f"{chunked['kahan_merged']['abs_error']}")
    print("  invariants:", {
        k: v for k, v in body["orderings"]["original"]["invariants"].items()
        if isinstance(v, bool)
    })

    _section("2. Small-number accumulation: [1, 2^-53 x 100]")
    u = 2.0 ** -53
    status, _, body = _post(
        base, "/api/v1/compare",
        {"values": [1.0] + [u] * 100, "orderings": ["original"]},
    )
    methods = body["orderings"]["original"]["methods"]
    print(f"  naive={methods['naive']['result']!r}  kahan={methods['kahan']['result']!r}")
    print(f"  exact (reference) = {body['reference']['value']!r}")

    _section("3. Alternating harmonic series, n=200000 (cancellation in sums)")
    status, _, body = _post(
        base, "/api/v1/scenario",
        {"scenario": "alternating", "n": 200_000,
         "orderings": ["original", "abs_ascending"]},
    )
    for ordering, entry in body["orderings"].items():
        naive = entry["methods"]["naive"]
        print(f"  {ordering:14s} naive abs error = {naive['abs_error']:.3e} "
              f"({naive['verdict']})")

    _section("4. Special-value policy and signed zeros")
    _, _, body = _post(base, "/api/v1/compare", {"values": [1.0, "Infinity", "-Infinity"]})
    print("  [1, +Inf, -Inf] ->", body["special_value_policy"]["note"])
    _, _, body = _post(base, "/api/v1/compare", {"values": [-0.0, -0.0]})
    print("  [-0.0, -0.0] naive result token:",
          body["orderings"]["original"]["methods"]["naive"]["result"])

    _section("5. Validation error envelope (specific failure category)")
    status, _, body = _post(base, "/api/v1/compare", {"values": [1.0, "oops"]})
    print(f"  HTTP {status} code={body['error']['code']}")
    print("  ", body["error"]["detail"][0]["msg"])

    _section("6. Client-supplied correlation id")
    status, headers, body = _post(
        base, "/api/v1/compare", {"values": [0.1] * 10},
        headers={"X-Request-Id": "demo-trace-001"},
    )
    print("  response header x-request-id:", headers.get("x-request-id"))
    print("  body request_id            :", body["request_id"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
