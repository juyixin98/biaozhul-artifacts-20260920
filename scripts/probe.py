#!/usr/bin/env python3
"""Probe a (lab or real) device and emit round-trip samples.

For each round we:
  1. optionally sleep ``uplink_delay`` seconds (simulated slow uplink);
  2. record t0 = time.time() immediately before POSTing;
  3. POST to the device's tick endpoint over real HTTP;
  4. record t3 = time.time() as soon as the response arrives;
  5. take c_recv/c_send from the response body.

Delay asymmetry is injected per request via ``--asym`` (a fraction of the
base delay moved from downlink to uplink) so the estimator's asymmetric error
bounds can be exercised.
"""

from __future__ import annotations

import argparse
import json
import random
import time

import httpx


def probe(base_url: str, device_id: str, n: int, base_delay: float,
          asym: float, jitter: float, processing: float,
          pace: float = 0.03,
          timeout: float = 5.0) -> list[dict]:
    url = f"{base_url.rstrip('/')}/lab/devices/{device_id}/tick"
    samples = []
    # asym in [-1, 1]: +1 => all delay on uplink; -1 => all on downlink.
    # trust_env=False keeps localhost traffic off any ambient SOCKS/HTTP proxy.
    with httpx.Client(timeout=timeout, trust_env=False) as client:
        for i in range(n):
            d = base_delay * (1.0 + random.uniform(-jitter, jitter))
            # asym=0 splits delay evenly; asym=1 puts it all on the uplink.
            up = max(0.0, d * (0.5 + 0.5 * asym))
            down = max(0.0, d - up)
            # t0 is taken before the emulated uplink delay, so the delay is
            # genuinely part of the measured round trip (t3 - t0).
            t0 = time.time()
            if up > 0:
                time.sleep(up)
            r = client.post(url, json={
                "downlink_delay": down,
                "processing_delay": processing,
            })
            t3 = time.time()
            r.raise_for_status()
            b = r.json()
            samples.append({
                "t0": t0,
                "t3": t3,
                "device_counter": b["c_recv"],
                "device_counter_send": b["c_send"],
                "seq": i,
            })
            # Pace requests so drift/offset accumulate observably.
            time.sleep(pace)
    return samples


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    ap.add_argument("--device", required=True)
    ap.add_argument("-n", "--count", type=int, default=24)
    ap.add_argument("--base-delay", type=float, default=0.02,
                    help="simulated one-way network delay (s)")
    ap.add_argument("--asym", type=float, default=0.0,
                    help="delay asymmetry in [-1, 1] (positive = slow uplink)")
    ap.add_argument("--jitter", type=float, default=0.3)
    ap.add_argument("--processing", type=float, default=0.002)
    ap.add_argument("--pace", type=float, default=0.03,
                    help="seconds between request starts (longer => clearer drift)")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    samples = probe(
        args.base_url, args.device, args.count, args.base_delay,
        args.asym, args.jitter, args.processing, args.pace,
    )
    payload = json.dumps({"samples": samples}, indent=2)
    if args.out:
        with open(args.out, "w") as f:
            f.write(payload)
        print(f"wrote {len(samples)} samples to {args.out}")
    else:
        print(payload)


if __name__ == "__main__":
    main()
