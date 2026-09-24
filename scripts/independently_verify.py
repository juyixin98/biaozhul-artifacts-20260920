#!/usr/bin/env python3
"""
independently_verify.py — independent trajectory checker.

It does NOT trust the server's own `verification` block.  Given the original
request and the server response, it:

  1. Re-derives every segment's path-speed ceiling V_k and acceleration
     ceiling A_k from the raw limits (including which joint is the bottleneck).
  2. Re-solves the time-optimal double-integrator law itself
     (bang-coast-bang / triangular) and compares durations, peak speeds and
     binding regimes with the server.
  3. Dense-samples the reconstructed CONTINUOUS law at >= `--samples` points
     per segment (in physical time) and checks |v_j(t)| <= v_max_j and
     |a_j(t)| <= a_max_j everywhere, including dwell handling.
  4. Checks strictly increasing timestamps and returned positions/velocities.

Usage: independently_verify.py request.json response.json [--samples 500]
Exit code 0 = verified, 1 = discrepancy found.
"""

import argparse
import json
import math
import sys

TOL = 1e-8


def die(msg):
    print(f"INDEPENDENT VERIFY FAILED: {msg}", file=sys.stderr)
    sys.exit(1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("request")
    ap.add_argument("response")
    ap.add_argument("--samples", type=int, default=500)
    args = ap.parse_args()

    req = json.load(open(args.request))
    resp = json.load(open(args.response))

    if args.samples < 2:
        die("samples must be >= 2")
    if not resp.get("ok"):
        die("response not ok: %s" % resp.get("error"))

    qs = req["waypoints"]
    vmax = [float(x) for x in req["velocity_limits"]]
    amax = [float(x) for x in req["acceleration_limits"]]
    dof = len(vmax)
    dwell = float(req.get("dwell_time", 1e-3))
    sv = req.get("start_velocity")
    ev = req.get("end_velocity")
    v_start = sv if sv is not None else [0.0] * dof
    v_end = ev if ev is not None else [0.0] * dof

    segs = resp["segments"]
    wps = resp["waypoints"]
    n = len(qs)
    if len(segs) != n - 1 or len(wps) != n:
        die("segment/waypoint counts")

    # ---- independently re-solve each segment ----
    t = 0.0
    times = [0.0]
    max_v_viol = 0.0
    max_a_viol = 0.0
    for k in range(n - 1):
        dq = [qs[k + 1][j] - qs[k][j] for j in range(dof)]
        L = math.sqrt(sum(x * x for x in dq))
        s = segs[k]

        if L == 0.0:
            if not s["zero_length"]:
                die(f"segment {k} should be flagged zero-length")
            if abs(s["duration"] - dwell) > 1e-15:
                die(f"segment {k} dwell duration")
            t += dwell
            times.append(t)
            continue

        if s["zero_length"]:
            die(f"segment {k} falsely flagged zero-length")

        u = [x / L for x in dq]
        moving = [j for j in range(dof) if abs(u[j]) > 1e-12]

        # ceilings and bottlenecks
        V = min(vmax[j] / abs(u[j]) for j in moving)
        A = min(amax[j] / abs(u[j]) for j in moving)
        jv = min(moving, key=lambda j: vmax[j] / abs(u[j]))
        ja = min(moving, key=lambda j: amax[j] / abs(u[j]))

        if abs(V - s["path_speed_ceiling"]) > 1e-9 * max(1.0, V):
            die(f"segment {k} velocity ceiling {V} vs {s['path_speed_ceiling']}")
        if abs(A - s["path_acceleration_ceiling"]) > 1e-9 * max(1.0, A):
            die(f"segment {k} acceleration ceiling")
        if jv != s["velocity_bottleneck_joint"]:
            die(f"segment {k} velocity bottleneck joint {jv} vs "
                f"{s['velocity_bottleneck_joint']}")
        if ja != s["acceleration_bottleneck_joint"]:
            die(f"segment {k} acceleration bottleneck joint")

        # boundary path speeds from requested Cartesian joint velocities
        if k == 0:
            s0 = sum(v_start[j] * u[j] for j in range(dof))
        else:
            s0 = 0.0
        if k == n - 2:
            s1 = sum(v_end[j] * u[j] for j in range(dof))
        else:
            s1 = 0.0

        # independently derived optimal law
        w_tri = math.sqrt(max(0.0, A * L + 0.5 * (s0 * s0 + s1 * s1)))
        if w_tri < V:
            w = w_tri
            t_acc = (w - s0) / A
            t_dec = (w - s1) / A
            d_acc = (w * w - s0 * s0) / (2 * A)
            d_cru = 0.0
            t_cru = 0.0
            regime = "acceleration"
        else:
            w = V
            t_acc = (V - s0) / A
            t_dec = (V - s1) / A
            d_acc = (V * V - s0 * s0) / (2 * A)
            d_dec = (V * V - s1 * s1) / (2 * A)
            d_cru = max(0.0, L - d_acc - d_dec)
            t_cru = d_cru / V
            regime = "velocity"
        T = t_acc + t_cru + t_dec

        if s["binding_regime"] != regime:
            die(f"segment {k} regime {regime} vs {s['binding_regime']}")
        if abs(T - s["duration"]) > 1e-9 * max(1.0, T):
            die(f"segment {k} duration {T} vs {s['duration']}")
        if abs(w - s["peak_path_speed"]) > 1e-8 * max(1.0, w):
            die(f"segment {k} peak speed")

        # ---- dense sampling in PHYSICAL TIME across the continuous law ----
        for m in range(args.samples):
            tau = T * m / (args.samples - 1)
            if tau < t_acc:
                sd = s0 + A * tau
                arc = s0 * tau + 0.5 * A * tau * tau
                sdd = A
            elif tau < t_acc + t_cru:
                sd = w
                arc = d_acc + w * (tau - t_acc)
                sdd = 0.0
            else:
                td = min(tau - t_acc - t_cru, t_dec)
                sd = w - A * td
                arc = d_acc + d_cru + w * td - 0.5 * A * td * td
                sdd = -A
            for j in moving:
                vj = sd * u[j]
                aj = sdd * u[j]
                if abs(vj) - vmax[j] > max_v_viol:
                    max_v_viol = max(max_v_viol, abs(vj) - vmax[j])
                if abs(aj) - amax[j] > max_a_viol:
                    max_a_viol = max(max_a_viol, abs(aj) - amax[j])
            # position must stay within segment bounds
            if arc < -1e-9 or arc > L + 1e-9:
                die(f"segment {k} sample arc {arc} out of [0,{L}]")

        t += T
        times.append(t)

    # ---- global checks against response ----
    tv = 1e-9 * max([1.0] + vmax)
    ta = 1e-9 * max([1.0] + amax)
    if max_v_viol > tv:
        die(f"dense velocity violation {max_v_viol} > {tv}")
    if max_a_viol > ta:
        die(f"dense acceleration violation {max_a_viol} > {ta}")

    for i in range(n):
        if abs(wps[i]["time"] - times[i]) > 1e-9 * max(1.0, times[i]):
            die(f"waypoint {i} time {times[i]} vs {wps[i]['time']}")
        for j in range(dof):
            if abs(wps[i]["position"][j] - qs[i][j]) > 1e-11:
                die(f"waypoint {i} position mismatch")
        if i > 0 and not wps[i]["time"] > wps[i - 1]["time"]:
            die("times not strictly increasing")

    # interior waypoints are full stops
    for i in range(1, n - 1):
        if any(abs(x) > 1e-10 for x in wps[i]["velocity"]):
            die(f"interior waypoint {i} not a stop")

    print(f"independent verification PASSED")
    print(f"  joints={dof} segments={n-1} dense_samples/segment={args.samples}")
    print(f"  total_time={times[-1]:.9f}s")
    print(f"  max dense velocity violation = {max_v_viol:.3e} (tol {tv:.1e})")
    print(f"  max dense acceleration violation = {max_a_viol:.3e} (tol {ta:.1e})")
    for k, s in enumerate(segs):
        tag = "dwell" if s["zero_length"] else (
            f"{s['binding_regime']}-bound "
            f"v:joint{s['velocity_bottleneck_joint']} "
            f"a:joint{s['acceleration_bottleneck_joint']}")
        print(f"  segment {k}: "
              f"t=[{wps[k]['time']:.6f},{wps[k+1]['time']:.6f}] {tag}")


if __name__ == "__main__":
    main()
