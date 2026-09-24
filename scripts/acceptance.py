#!/usr/bin/env python3
"""End-to-end acceptance checks for the joint-trajectory TOPP service.

Runs, in order:
  1. ctest unit suite (analytic single-axis, multi-axis bottlenecks, reverse,
     short segment, duplicates, infeasible boundary, cusp, sha256 vectors).
  2. CLI analytic-duration checks against closed-form bang-bang answers.
  3. Independent re-verification of EVERY dense waypoint in the response
     (strictly increasing time; finite states; limits respected).
  4. Independent recomputation of the SHA-256 response checksum.
  5. Infeasible request returns HTTP-style 422 code through the CLI.
  6. (unless --no-http) boots the real HTTP server and exercises /healthz and
     /parameterize over a TCP socket using only the stdlib.

Exits non-zero on the first failed group.
"""
import argparse
import hashlib
import json
import math
import re
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build"
CLI = BUILD / "topp_cli"
SERVER = BUILD / "topp_server"

FAILS = []


def check(cond, label):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    if not cond:
        FAILS.append(label)


def run_cli(obj):
    p = subprocess.run([str(CLI), "-"], input=json.dumps(obj),
                       capture_output=True, text=True)
    return p.returncode, json.loads(p.stdout) if p.stdout.strip() else {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-http", action="store_true")
    ap.add_argument("--port", type=int, default=8097)
    args = ap.parse_args()

    # 1. unit tests
    print("== 1. unit tests (ctest) ==")
    r = subprocess.run(["ctest", "--output-on-failure"], cwd=BUILD)
    check(r.returncode == 0, "ctest unit suite")

    # 2. CLI analytic durations
    print("== 2. analytic durations via CLI ==")
    def duration(points, v, a, **kw):
        req = {"joint_names": [f"j{i}" for i in range(len(v))],
               "points": points, "velocity_limits": v,
               "acceleration_limits": a, "grid_cells": 2000,
               "samples_per_cell": 6}
        req.update(kw)
        rc, out = run_cli(req)
        assert rc == 0, out
        return out

    b = duration([[0], [10]], [2], [1])
    check(abs(b["duration"] - 7.0) < 5e-3, f"bang-bang T=7 (got {b['duration']:.4f})")
    t = duration([[0], [1]], [100], [1])
    check(abs(t["duration"] - 2.0) < 5e-3, f"triangle T=2 (got {t['duration']:.4f})")
    r2 = duration([[0], [-10]], [2], [1])
    check(abs(r2["duration"] - 7.0) < 5e-3, f"reverse T=7 (got {r2['duration']:.4f})")
    m = duration([[0, 0], [10, 5]], [3, 1], [1, 2])
    check(abs(m["duration"] - 7.0) < 6e-3, f"multi-axis shared clock T=7 (got {m['duration']:.4f})")
    check(m["bottleneck_velocity_joint"] == 1, "velocity bottleneck joint = 1 (slow axis)")
    check(m["bottleneck_acceleration_joint"] == 0, "acceleration bottleneck joint = 0")

    # 3. independent re-verification of response states
    print("== 3. independent state verification (basic example) ==")
    req = json.loads((ROOT / "examples/request_basic.json").read_text())
    rc, out = run_cli(req)
    check(rc == 0, "basic example accepted")
    dof = len(req["joint_names"])
    vmax = req["velocity_limits"]; amax = req["acceleration_limits"]
    prev_t = -1.0
    ok_mono = True; ok_lim = True; ok_n = True
    check(len(out["points"]) ==
          len(req["points"]) - out["removed_duplicates"],
          "reported point count after dedupe")
    for st in out["points"]:
        if not st["t"] > prev_t:
            ok_mono = False
        prev_t = st["t"]
        for j in range(dof):
            if abs(st["qd"][j]) > vmax[j] * 1.005 + 1e-9: ok_lim = False
            if abs(st["qdd"][j]) > amax[j] * 1.005 + 1e-9: ok_lim = False
            if not (math.isfinite(st["q"][j]) and math.isfinite(st["qd"][j])
                    and math.isfinite(st["qdd"][j])):
                ok_n = False
    check(ok_mono, "times strictly increasing")
    check(ok_lim, "waypoint velocities/accelerations within limits")
    check(ok_n, "all states finite")
    check(abs(out["points"][0]["t"]) < 1e-9, "first time is 0")
    check(abs(out["points"][-1]["t"] - out["duration"]) < 1e-9,
          "last time equals duration")
    check(out["verification"]["passed"], "server-side dense verification passed")
    check(len(out["active_intervals"]) > 0, "active-limit intervals reported")
    print("       bottleneck velocity joint:",
          out["bottleneck_velocity_joint"],
          " acceleration joint:", out["bottleneck_acceleration_joint"])

    # 4. checksum
    print("== 4. SHA-256 checksum ==")
    tag = out["checksum"]
    payload = {k: v for k, v in out.items() if k != "checksum"}

    # Normalize every float to %.17g, the exact canonical spelling the server
    # uses (round-trips IEEE-754 doubles; independent of repr() differences).
    def canon(obj):
        if isinstance(obj, float):
            return format(obj, ".17g")
        if isinstance(obj, dict):
            return {k: canon(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [canon(v) for v in obj]
        return obj
    norm = canon(payload)
    text = json.dumps(norm, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)
    # json quotes the number-strings; the server hashes them as raw JSON
    # numbers, so strip those quotes back off.
    text = re.sub(r'"(-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?)"',
                  r"\1", text)
    expect = "sha256:" + hashlib.sha256(text.encode()).hexdigest()
    check(tag == expect, "checksum verifies over canonical body")

    # duplicates example
    print("== 4b. duplicate handling ==")
    dup = json.loads((ROOT / "examples/request_duplicates.json").read_text())
    rc, d = run_cli(dup)
    check(rc == 0 and d["removed_duplicates"] == 2,
          f"two duplicates removed (got {d.get('removed_duplicates')})")
    check(d["retained_input_indices"] == [0, 2, 3, 5],
          f"retained indices {d.get('retained_input_indices')}")

    # 5. infeasible boundary -> 422 class
    print("== 5. infeasible boundary ==")
    inf = json.loads(
        (ROOT / "examples/request_infeasible_boundary.json").read_text())
    rc, out = run_cli(inf)
    check(rc == 4, f"CLI exit code 4xx (got {rc})")
    check(out.get("error") == "INFEASIBLE_BOUNDARY",
          f"error code INFEASIBLE_BOUNDARY (got {out.get('error')})")
    rc, out = run_cli({"joint_names": ["j"], "points": [[1], [1], [1]],
                       "velocity_limits": [1], "acceleration_limits": [1]})
    check(rc == 4 and out.get("error") == "INFEASIBLE_LIMITS",
          "all-duplicate points -> INFEASIBLE_LIMITS")

    # 6. HTTP end-to-end
    if not args.no_http:
        print("== 6. HTTP server end-to-end ==")
        proc = subprocess.Popen([str(SERVER), "--host", "127.0.0.1",
                                 "--port", str(args.port)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            # wait for listen
            for _ in range(50):
                try:
                    with socket.create_connection(("127.0.0.1", args.port), 0.2):
                        break
                except OSError:
                    time.sleep(0.1)

            def http(method, path, body=None):
                req_bytes = b""
                if body is not None:
                    req_bytes = json.dumps(body).encode()
                with socket.create_connection(("127.0.0.1", args.port), 5) as s:
                    if body is not None:
                        s.sendall(
                            f"POST {path} HTTP/1.1\r\nHost: localhost\r\n"
                            f"Content-Type: application/json\r\n"
                            f"Content-Length: {len(req_bytes)}\r\n\r\n"
                            .encode() + req_bytes)
                    else:
                        s.sendall(f"GET {path} HTTP/1.1\r\nHost: localhost\r\n"
                                  f"Content-Length: 0\r\n\r\n".encode())
                    chunks = []
                    while True:
                        d = s.recv(65536)
                        if not d:
                            break
                        chunks.append(d)
                raw = b"".join(chunks)
                head, _, rb = raw.partition(b"\r\n\r\n")
                status = int(head.split()[1])
                return status, json.loads(rb.decode())

            st, h = http("GET", "/healthz")
            check(st == 200 and h["status"] == "ok", f"GET /healthz -> {st}")
            check(h.get("checksum", "").startswith("sha256:"),
                  "health payload checksummed")
            st, o = http("POST", "/parameterize",
                         json.loads((ROOT / "examples/request_basic.json")
                                    .read_text()))
            check(st == 200 and o["verification"]["passed"],
                  f"POST /parameterize -> {st}")
            st, o = http("POST", "/parameterize", inf)
            check(st == 422 and o["error"] == "INFEASIBLE_BOUNDARY",
                  f"POST infeasible -> 422 (got {st})")
            st, _ = http("GET", "/nope")
            check(st == 404, f"unknown path -> 404 (got {st})")
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    print("\n" + ("ALL ACCEPTANCE CHECKS PASSED" if not FAILS
                  else f"{len(FAILS)} FAILURES"))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
