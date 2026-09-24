#!/usr/bin/env python3
"""End-to-end acceptance demo against a running server.

Scenarios (each is a separate lab device):
  1. asym        - strongly asymmetric one-way delays; bounds stay valid
  2. wrap        - 16-bit microsecond counter rolls over mid-run
  3. reboot      - device restarts; counter reset detected, segments split
  4. drift       - oscillator ppm changes mid-run; drift_change detected
  5. few         - only 3 samples -> status must be uncertain

Prerequisite:  uvicorn app.main:app --port 8000
Run:           python scripts/demo.py
"""

from __future__ import annotations

import os
import sys

import httpx

from probe import probe

BASE = os.environ.get("CLOCKDRIFT_BASE", "http://127.0.0.1:8000")
HZ = 1_000_000.0


def post(client: httpx.Client, path: str, json_body=None, **params):
    return client.post(f"{BASE}{path}", json=json_body or {}, params=params)


def section(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def summarize(name: str, resp: dict) -> None:
    print(f"[{name}] status={resp['status']} published={resp['published']}")
    if resp.get("reason"):
        print(f"  reason: {resp['reason']}")
    for s in resp["segments"]:
        extra = ""
        if s.get("drift_ppm") is not None:
            extra = f" drift={s['drift_ppm']:+.1f}ppm"
        if s.get("offset_interval"):
            extra += (f" offset_interval=[{s['offset_interval']['lower']*1e3:+.2f},"
                      f" {s['offset_interval']['upper']*1e3:+.2f}] ms")
        print(f"  seg {s['index']}: {s['status']} n={s['n_samples_used']}/"
              f"{s['n_samples_input']} slope={s['slope']}{extra}")
        if s.get("reason"):
            print(f"    note: {s['reason']}")
    for d in resp["discontinuities"]:
        print(f"  !! {d['type']} @host={d['at_host_time']:.3f}: {d['evidence']}")


def ingest(client, device, modulus, hz, samples, publish=True):
    body = {
        "device_id": device,
        "counter_modulus": modulus,
        "counter_nominal_hz": hz,
        "samples": samples,
    }
    r = post(client, f"/api/v1/devices/{device}/samples", body, publish=publish)
    r.raise_for_status()
    return r.json()


def main() -> int:
    with httpx.Client(timeout=30, trust_env=False) as c:
        # The lab device registry is process-global. Clear any devices left by
        # a previous demo run against the same server so results are clean.
        for dev in ("dev-asym", "dev-wrap", "dev-reboot", "dev-drift",
                    "dev-few"):
            c.delete(f"{BASE}/lab/devices/{dev}")

        # ---- 1. asymmetric delay ----------------------------------------
        section("1. Asymmetric one-way delays (slow uplink, asym=+0.8)")
        post(c, "/lab/devices", {"device_id": "dev-asym", "ppm": 120.0})
        s = probe(BASE, "dev-asym", n=24, base_delay=0.02, asym=0.8,
                  jitter=0.3, processing=0.001)
        r = ingest(c, "dev-asym", None, HZ, s)
        summarize("asym", r)
        first_version = r["version"]
        mid_c = s[len(s) // 2]["device_counter"]
        mid_t = s[len(s) // 2]["t0"]
        cv = post(c, "/api/v1/convert", {
            "device_id": "dev-asym", "device_counter": mid_c,
            "host_time_hint": mid_t}).json()
        print(f"  convert -> {cv['status']} point={cv['host_time_point']:.6f} "
              f"interval=[{cv['host_time_interval']['lower']:.6f}, "
              f"{cv['host_time_interval']['upper']:.6f}] version={cv['version']}")
        w = (cv["host_time_interval"]["upper"]
             - cv["host_time_interval"]["lower"])
        print(f"  interval width={w*1000:.2f} ms (must be > 0, no symmetry assumed)")

        # ---- 2. counter wrap --------------------------------------------
        # 16-bit microsecond counter = 65.5 ms period. Pace slower than the
        # period (90 ms) so wraps are isolated, one-per-gap, and clearly
        # kinematic rather than several per interval.
        section("2. Counter wraparound (modulus 2^16 microseconds)")
        M = float(2 ** 16)
        post(c, "/lab/devices", {"device_id": "dev-wrap", "ppm": 50.0,
                                 "modulus": M})
        s = probe(BASE, "dev-wrap", n=24, base_delay=0.004, asym=0.0,
                  jitter=0.3, processing=0.001, pace=0.09)
        r = ingest(c, "dev-wrap", M, HZ, s)
        summarize("wrap", r)
        assert all(d["type"] == "wrap" for d in r["discontinuities"]), \
            "isolated wraparounds must not be read as reboots/time jumps"
        assert r["segments"] and r["segments"][0]["status"] == "calibrated", \
            "wraps keep one continuous regime"

        # ---- 3. reboot ---------------------------------------------------
        section("3. Device reboot (counter resets to ~0)")
        post(c, "/lab/devices", {"device_id": "dev-reboot", "ppm": 80.0})
        s1 = probe(BASE, "dev-reboot", n=12, base_delay=0.005, asym=0.2,
                   jitter=0.3, processing=0.001)
        post(c, "/lab/devices/dev-reboot/reboot")
        s2 = probe(BASE, "dev-reboot", n=12, base_delay=0.005, asym=0.2,
                   jitter=0.3, processing=0.001)
        r = ingest(c, "dev-reboot", None, HZ, s1 + s2)
        summarize("reboot", r)
        print("  expected: >=2 segments, a 'reboot' discontinuity between them")

        # ---- 4. drift change ---------------------------------------------
        # A slope break is only identifiable when it stands above the noise:
        # the regression's slope standard error scales with RTT/span. Use a
        # tight (~2 ms) link and a ~5 s span per regime. With a loose link or
        # short span the service deliberately reports NO changepoint (and,
        # where the span gate fails, status=uncertain) rather than guess.
        section("4. Drift change (+200ppm -> -300ppm), tight link, long span")
        post(c, "/lab/devices", {"device_id": "dev-drift", "ppm": 200.0})
        s1 = probe(BASE, "dev-drift", n=20, base_delay=0.001, asym=0.0,
                   jitter=0.1, processing=0.0005, pace=0.5)
        # Change the oscillator rate and confirm the simulator took it.
        for _ in range(3):
            post(c, "/lab/devices/dev-drift/drift", None, ppm=-300.0)
            state = c.get(f"{BASE}/lab/devices/dev-drift").json()
            if abs(state["ppm"] - (-300.0)) < 1e-9:
                break
        assert abs(state["ppm"] - (-300.0)) < 1e-9, "lab device did not adopt new drift"
        s2 = probe(BASE, "dev-drift", n=20, base_delay=0.001, asym=0.0,
                   jitter=0.1, processing=0.0005, pace=0.5)
        r = ingest(c, "dev-drift", None, HZ, s1 + s2)
        summarize("drift", r)
        assert any(d["type"] == "drift_change" for d in r["discontinuities"]), \
            "a real, noise-resolvable slope break must be detected"
        print("  expected: a 'drift_change' discontinuity with two regimes")

        # ---- 5. few samples ----------------------------------------------
        section("5. Few samples (n=3) -> uncertain, nothing published")
        post(c, "/lab/devices", {"device_id": "dev-few", "ppm": 100.0})
        s = probe(BASE, "dev-few", n=3, base_delay=0.005, asym=0.0,
                  jitter=0.2, processing=0.001)
        r = ingest(c, "dev-few", None, HZ, s)
        summarize("few", r)
        assert r["published"] is False, "must not publish on weak evidence"

        # ---- model inventory + signature verification --------------------
        section("6. Model versions and signature verification")
        models = c.get(f"{BASE}/api/v1/models").json()
        for m in models:
            print(f"  {m['device_id']:12s} {m['version']} "
                  f"valid_from={m['valid_from_host_time']:.3f} "
                  f"valid_to={m['valid_to_host_time']} "
                  f"superseded={m['superseded']}")
        if models:
            v = models[0]["version"]
            vr = c.get(f"{BASE}/api/v1/models/{v}/verify").json()
            print(f"  verify {v}: valid={vr['valid']} ({vr['detail']})")

        # ---- republish closes the previous validity window ---------------
        section("7. Republish: new model closes the old validity interval")
        s = probe(BASE, "dev-asym", n=12, base_delay=0.02, asym=-0.5,
                  jitter=0.3, processing=0.001)
        r = ingest(c, "dev-asym", None, HZ, s)
        assert r["published"] and r["version"] != first_version, r
        hist = c.get(f"{BASE}/api/v1/devices/dev-asym/models").json()
        for m in hist:
            print(f"  {m['version']}: valid_to={m['valid_to_host_time']} "
                  f"superseded={m['superseded']}")
        assert hist[0]["superseded"] is True and hist[0]["valid_to_host_time"]
        assert hist[-1]["valid_to_host_time"] is None
        print("\nALL SCENARIOS EXECUTED.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
