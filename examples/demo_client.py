#!/usr/bin/env python3
"""End-to-end acceptance demo for the async EKF fusion service.

It performs the following *real* operations against a running server (it will
start ``uvicorn`` itself on 127.0.0.1:8765 if none is reachable):

1. creates two sessions with identical configuration;
2. sends the example measurements to session A in the shuffled arrival order
   (bounded <= 2 s lateness -> checkpointed replays) and to session B sorted
   by measurement time (no replay) -- the two resulting state traces must be
   bit-for-bit identical;
3. prints every fusion step (time, kind, state, innovation, NIS, accepted);
4. shows the retained rejection evidence (gross GNSS outlier, bad covariance);
5. checks the SHA-256 step hash chain and tries a genuine late-too-old and
   duplicate rejection;
6. compares the final estimate with the known ground truth.

A full machine-readable report is written to ``demo_report.json``.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def http(method: str, url: str, payload: dict | None = None,
         expect: tuple[int, ...] = (200, 201)) -> tuple[int, dict]:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("content-type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = json.loads(e.read())
        if e.code not in expect:
            raise RuntimeError(f"{method} {url} -> {e.code}: {body}")
        return e.code, body


def wait_for_port(host: str, port: int, timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.25)
    raise TimeoutError(f"server did not open {host}:{port}")


def is_our_server(base: str) -> bool:
    """True only if the reachable server exposes THIS app (the /health
    response carries our version and a sessions field).  An unrelated server
    squatting the chosen port must not be mistaken for ours."""
    try:
        _, body = http("GET", f"{base}/health")
    except Exception:
        return False
    return body.get("status") == "ok" and "sessions" in body and "version" in body


def free_port(preferred: int) -> int:
    for port in [preferred] + list(range(8800, 8900)):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("no free port found")


def maybe_start_server(base: str) -> tuple[subprocess.Popen | None, str]:
    host, port = base.replace("http://", "").split(":")
    port = int(port)
    if is_our_server(base):
        return None, base
    # something else is listening (or nothing): pick a free port we own
    port = free_port(port)
    base = f"http://{host}:{port}"
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "ekf_fusion.app:app",
         "--host", host, "--port", str(port)],
        cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, env=env,
    )
    wait_for_port(host, port)
    if not is_our_server(base):
        proc.kill()
        raise RuntimeError(f"server on {base} did not become our app")
    return proc, base


def create_session(base: str) -> str:
    _, body = http("POST", f"{base}/sessions", {})
    return body["session_id"]


def send_one(base: str, sid: str, m: dict) -> dict:
    status, body = http("POST", f"{base}/sessions/{sid}/measurements", m,
                        expect=(200, 201, 400, 401, 422))
    body["_http_status"] = status
    return body


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8765")
    ap.add_argument("--input", default=str(HERE / "measurements.json"))
    args = ap.parse_args()

    proc, args.base = maybe_start_server(args.base)
    base = args.base
    print(f"using server at {base}"
          + (" (started by demo)" if proc else " (already running)"))
    try:
        data = json.loads(Path(args.input).read_text())
        msgs = data["measurements"]
        sorted_msgs = sorted(
            [dict(m) for m in msgs if m["message_id"] != "m-bad-cov"],
            key=lambda m: (m["t"], 0 if m["kind"] == "odom" else 1),
        )

        sid_shuf = create_session(base)
        sid_sort = create_session(base)

        print(f"== sending {len(msgs)} messages in shuffled arrival order "
              f"(session {sid_shuf}) ==")
        n_replay = 0
        shuffled_outcomes = []
        for i, m in enumerate(msgs):
            out = send_one(args.base, sid_shuf, m)
            shuffled_outcomes.append(out)
            if out.get("replayed"):
                n_replay += 1
            tag = out.get("status")
            extra = out.get("reason") or ""
            step = out.get("step")
            if step:
                x, y, vx, vy = step["x"]
                nu = step["innovation"]
                extra = (f"{extra:18s} NIS={step['nis']:7.3f} "
                         f"nu=({nu[0]:+.3f},{nu[1]:+.3f}) "
                         f"p=({x:6.3f},{y:6.3f}) v=({vx:5.3f},{vy:5.3f})")
            print(f"  [{i:3d}] t={m['t']:5.2f} {m['kind']:4s} {tag:8s} {extra}")

        print(f"\n== replaying same data sorted by measurement time "
              f"(session {sid_sort}) ==")
        for m in sorted_msgs:
            send_one(args.base, sid_sort, m)

        _, steps_shuf = http("GET", f"{args.base}/sessions/{sid_shuf}/steps")
        _, steps_sort = http("GET", f"{args.base}/sessions/{sid_sort}/steps")
        _, rej_shuf = http("GET",
                           f"{args.base}/sessions/{sid_shuf}/rejections")
        _, integ = http("GET",
                        f"{args.base}/sessions/{sid_shuf}/integrity")
        _, state = http("GET",
                        f"{args.base}/sessions/{sid_shuf}/state")

        # seq is the arrival-order diagnostic and legitimately differs between
        # sessions; every other field (incl. hash chain) must be identical.
        def _core(steps):
            return [{k: v for k, v in s.items() if k != "seq"}
                    for s in steps["steps"]]

        trace_match = _core(steps_shuf) == _core(steps_sort)
        print(f"\ncheckpointed replays observed : {n_replay}")
        print(f"accepted steps                : {len(steps_shuf['steps'])}")
        print(f"rejected (outlier evidence)   : {len(rej_shuf['rejections'])}")
        print(f"hash chain verified           : {integ['ok']}")
        print(f"shuffled trace == sorted trace: {trace_match}")

        print("\noutlier / invalid evidence:")
        for r in rej_shuf["rejections"]:
            print(f"  {r['message_id']} t={r['t']:.2f} {r['kind']} "
                  f"reason={r['reason']} NIS={r['nis']:.3f} "
                  f"gate={r['gate']:.3f} innovation={r['innovation']}")

        # late-too-old: high-water is ~10 s; jump far back beyond 2 s window
        too_old = {"message_id": "m-late-old", "t": 0.001, "kind": "gnss",
                   "z": [0.0, 0.0], "R": [[1.0, 0.0], [0.0, 1.0]]}
        out = send_one(args.base, sid_shuf, too_old)
        print(f"\ntime jump-back beyond window -> {out['status']} "
              f"({out.get('reason')}), hwm={out.get('high_water_time')}")

        # duplicate id
        dup = dict(sorted_msgs[5])
        out = send_one(args.base, sid_shuf, dup)
        print(f"duplicate message id         -> {out['status']} "
              f"({out.get('reason')})")

        v = data["truth"]["v"]
        p0 = data["truth"]["p0"]
        est = state["position"]
        estv = state["velocity"]
        t_end = state["t"]
        truth_pos = [p0[0] + v[0] * t_end, p0[1] + v[1] * t_end]
        perr = ((est[0] - truth_pos[0]) ** 2 + (est[1] - truth_pos[1]) ** 2) ** 0.5
        verr = ((estv[0] - v[0]) ** 2 + (estv[1] - v[1]) ** 2) ** 0.5
        print(f"\nfinal t={t_end:.2f}")
        print(f"  estimated position {est}  truth {truth_pos}  |err|={perr:.3f} m")
        print(f"  estimated velocity {estv}  truth {v}  |err|={verr:.3f} m/s")

        ok = (
            trace_match
            and integ["ok"]
            and any(r["reason"] == "outlier_gate" for r in rej_shuf["rejections"])
            and len(steps_shuf["steps"]) == len(steps_sort["steps"])
            and n_replay > 0
            and perr < 1.0
            and verr < 0.2
        )

        # the invalid covariance item is a per-item rejection, visible in
        # outcomes (it is never ingested into the timeline)
        cov_outcome = next(
            (o for o in shuffled_outcomes
             if o.get("message_id") == "m-bad-cov"), None)
        cov_reason = cov_outcome.get("reason") or cov_outcome.get("error") \
            if cov_outcome else None
        print(f"\nasymmetric covariance response : {cov_reason or 'MISSING'}")
        ok = ok and cov_reason == "covariance_not_symmetric"

        report = {
            "ok": ok,
            "n_replays": n_replay,
            "trace_match": trace_match,
            "integrity": integ,
            "state": state,
            "truth_position_at_t": truth_pos,
            "position_error_m": perr,
            "velocity_error_mps": verr,
            "rejections": rej_shuf["rejections"],
            "steps_shuffled": steps_shuf["steps"],
            "outcomes": [
                {k: v for k, v in o.items() if k != "step"}
                for o in shuffled_outcomes
            ],
        }
        (HERE / "demo_report.json").write_text(json.dumps(report, indent=2))
        print(f"\nfull report -> {HERE / 'demo_report.json'}")
        print("RESULT:", "PASS" if ok else "FAIL")
        return 0 if ok else 1
    finally:
        if proc is not None:
            proc.send_signal(signal.SIGINT)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
