#!/usr/bin/env python3
"""Differential tests: KD-tree backend vs exhaustive scan.

Runs the compiled ./spatial_index binary on generated requests and compares
every result against a Python brute-force reference. Random inputs use fixed
seeds for reproducibility.

The reference works with Python Fraction for squared distances so that very
large integer coordinates (up to +/-10^15) are compared exactly; float
coordinates are compared with the binary64 values the C++ side parses too.
"""

import json
import math
import os
import random
import subprocess
import sys
from fractions import Fraction

BIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "spatial_index")

passed = 0
failed = 0
failures = []


def run(request, expect_error=False):
    proc = subprocess.run(
        [BIN, "-"], input=json.dumps(request), capture_output=True, text=True
    )
    # Malformed requests are reported as a JSON error body with exit code 1;
    # both cases must still parse as JSON for the assertions below.
    if proc.returncode != 0 and not expect_error:
        raise AssertionError(
            f"binary exited {proc.returncode}: {proc.stdout} {proc.stderr}"
        )
    if not proc.stdout.strip():
        raise AssertionError(f"empty stdout (exit {proc.returncode}): {proc.stderr}")
    return json.loads(proc.stdout)


def ref_knn(points, qx, qy, k):
    qx, qy = Fraction(float(qx)), Fraction(float(qy))
    scored = []
    for pid, x, y in points:
        x = Fraction(float(x))
        y = Fraction(float(y))
        d2 = (x - qx) ** 2 + (y - qy) ** 2
        scored.append((d2, pid))
    scored.sort(key=lambda t: (t[0], t[1]))
    return scored[: min(k, len(scored))]


def ref_radius(points, qx, qy, r):
    qx, qy = Fraction(float(qx)), Fraction(float(qy))
    r2 = Fraction(float(r)) ** 2
    out = []
    for pid, x, y in points:
        x = Fraction(float(x))
        y = Fraction(float(y))
        d2 = (x - qx) ** 2 + (y - qy) ** 2
        if d2 <= r2:
            out.append((d2, pid))
    out.sort(key=lambda t: (t[0], t[1]))
    return out


def check_case(name, points, queries, expect_error=False):
    global passed, failed
    req = {
        "points": [{"id": pid, "x": x, "y": y} for pid, x, y in points],
        "queries": queries,
    }
    try:
        resp = run(req, expect_error=expect_error)
    except Exception as e:  # noqa: BLE001
        failed += 1
        failures.append(f"{name}: execution error: {e}")
        return
    if expect_error:
        if resp.get("ok"):
            failed += 1
            failures.append(f"{name}: expected error, got ok response")
        else:
            passed += 1
        return
    if not resp.get("ok"):
        failed += 1
        failures.append(f"{name}: unexpected error: {resp.get('error')}")
        return

    results = resp["results"]
    if len(results) != len(queries):
        failed += 1
        failures.append(f"{name}: result count {len(results)} != queries {len(queries)}")
        return

    ok = True
    for qi, (q, got) in enumerate(zip(queries, results)):
        if q["type"] == "knn":
            want = ref_knn(points, q["x"], q["y"], q["k"])
        else:
            want = ref_radius(points, q["x"], q["y"], q["r"])

        got_list = got["neighbors"]
        if len(got_list) != len(want):
            ok = False
            failures.append(
                f"{name} q{qi} ({q}): count {len(got_list)} != reference {len(want)}"
            )
            break
        for rank, ((wd2, wid), item) in enumerate(zip(want, got_list)):
            if item["id"] != wid:
                ok = False
                failures.append(
                    f"{name} q{qi} rank {rank}: id {item['id']} != reference {wid} "
                    f"(dist2 ref={float(wd2):.17g}, got={item['distance_squared']})"
                )
                break
            # C++ reports long double arithmetic; reference is Fraction over
            # binary64 inputs. They share inputs exactly; compare tightly.
            gd2 = Fraction(str(item["distance_squared"]))
            rel = abs(gd2 - wd2) / max(abs(wd2), Fraction(1))
            # Integer coordinates compare exactly; float inputs can differ by
            # one binary64 ulp between long double math and exact rationals.
            if float(rel) > 1e-12:
                ok = False
                failures.append(
                    f"{name} q{qi} rank {rank} id {wid}: dist2 mismatch "
                    f"got={item['distance_squared']} ref={float(wd2):.21g}"
                )
                break
        if not ok:
            break
        if got.get("returned") != len(want):
            ok = False
            failures.append(f"{name} q{qi}: returned field {got.get('returned')} != {len(want)}")
            break

    if ok:
        passed += 1
    else:
        failed += 1


def rand_points(n, coord_fn, id_start=0):
    return [(id_start + i, coord_fn(), coord_fn()) for i in range(n)]


def main():
    # 1. Empty index.
    check_case("empty-index", [], [
        {"type": "knn", "x": 0, "y": 0, "k": 3},
        {"type": "radius", "x": 0, "y": 0, "r": 5},
    ])

    # 2. Single point.
    check_case("single", [(1, 1.5, -2.25)], [
        {"type": "knn", "x": 0, "y": 0, "k": 1},
        {"type": "knn", "x": 0, "y": 0, "k": 5},  # k > size
        {"type": "radius", "x": 1.5, "y": -2.25, "r": 0},  # exact hit, r=0
        {"type": "radius", "x": 1.5, "y": -2.26, "r": 0},
    ])

    # 3. Duplicate coordinates with distinct ids: ties must order by id.
    dups = [(7, 0.0, 0.0), (3, 0.0, 0.0), (9, 0.0, 0.0), (1, 0.0, 0.0),
            (5, 1.0, 0.0)]
    check_case("duplicate-coords", dups, [
        {"type": "knn", "x": 0, "y": 0, "k": 4},
        {"type": "knn", "x": 0, "y": 0, "k": 10},
        {"type": "radius", "x": 0, "y": 0, "r": 0},
        {"type": "radius", "x": 0, "y": 0, "r": 1},
    ])

    # 4. Collinear points (all on y=3 and on x=7): degenerate partitions.
    line_h = [(100 + i, float(i), 3.0) for i in range(50)]
    line_v = [(200 + i, 7.0, float(i) * 0.5 - 12.5) for i in range(50)]
    check_case("collinear", line_h + line_v, [
        {"type": "knn", "x": 25, "y": 3, "k": 7},
        {"type": "knn", "x": 7, "y": 0, "k": 5},
        {"type": "knn", "x": 7, "y": 3, "k": 20},  # equal distance pairs -> id order
        {"type": "radius", "x": 7, "y": 3, "r": 5.0000000001},
        {"type": "radius", "x": 25, "y": 3, "r": 2},
    ])

    # 5. k exceeds total on random sets; k=0.
    rng = random.Random(424242)
    random_sets = []
    for trial in range(6):
        n = rng.randint(1, 120)
        pts = rand_points(n, lambda: rng.uniform(-1000, 1000))
        random_sets.append(pts)
        qs = [
            {"type": "knn", "x": rng.uniform(-1000, 1000),
             "y": rng.uniform(-1000, 1000), "k": rng.choice([0, 1, 2, n, n + 5, 500])},
            {"type": "radius", "x": rng.uniform(-1000, 1000),
             "y": rng.uniform(-1000, 1000), "r": rng.choice([0.0, 50.0, 250.0, 5000.0])},
        ]
        check_case(f"random-float-{trial}", pts, qs)

    # 6. Huge integer coordinates (up to +-1e15) including equal-distance rings.
    big = []
    rng2 = random.Random(99)
    for i in range(300):
        big.append((i, rng2.randint(-10**15, 10**15), rng2.randint(-10**15, 10**15)))
    # axis-aligned ring at exact squared distance 25 around the origin
    ring = [(1000, 3, 4), (1001, 4, 3), (1002, -3, 4), (1003, -4, 3),
            (1004, 3, -4), (1005, 4, -3), (1006, -3, -4), (1007, -4, -3),
            (1008, 5, 0), (1009, 0, 5), (1010, -5, 0), (1011, 0, -5)]
    bigq = [
        {"type": "knn", "x": 0, "y": 0, "k": 12},
        {"type": "knn", "x": 10**15, "y": -10**15, "k": 20},
        {"type": "radius", "x": 0, "y": 0, "r": 5},   # closed-ball boundary
        {"type": "radius", "x": 0, "y": 0, "r": 4.999999999},
        {"type": "knn", "x": 777000000000001, "y": -333000000000001, "k": 30},
    ]
    check_case("huge-coords", big + ring, bigq)

    # 7. Grid: tons of equidistant symmetry, tie-break stress.
    grid = [(g * 20 + c + 5000, c - 10 + 0.25, g - 10 - 0.25)
            for g in range(20) for c in range(20)]
    check_case("grid", grid, [
        {"type": "knn", "x": 0.25, "y": -0.25, "k": 36},
        {"type": "radius", "x": 0.25, "y": -0.25, "r": math.sqrt(8) + 1e-12},
        {"type": "knn", "x": 2.25, "y": -1.25, "k": 1},
    ])

    # 8. Coincident query points and negative k / bad input handling.
    check_case("bad-k", [(1, 0, 0)], [
        {"type": "knn", "x": 0, "y": 0, "k": -1}], expect_error=True)
    check_case("dup-id", [(1, 0, 0), (1, 2, 2)], [], expect_error=True)
    check_case("bad-type", [(1, 0, 0)], [
        {"type": "frobnicate", "x": 0, "y": 0}], expect_error=True)
    check_case("missing-points", [], [
        {"type": "knn", "x": 0, "y": 0, "k": 1}])  # empty is legal

    # 9. Large randomized differential run.
    pts = rand_points(2000, lambda: rng.uniform(-1e6, 1e6), id_start=9000)
    big_qs = []
    for _ in range(25):
        big_qs.append({"type": "knn", "x": rng.uniform(-1e6, 1e6),
                       "y": rng.uniform(-1e6, 1e6),
                       "k": rng.choice([1, 3, 10, 100, 1999, 2000, 5000])})
        big_qs.append({"type": "radius", "x": rng.uniform(-1e6, 1e6),
                       "y": rng.uniform(-1e6, 1e6),
                       "r": rng.choice([0.0, 100.0, 1e5])})
    check_case("large-random-2000", pts, big_qs)

    print(f"PASSED {passed}  FAILED {failed}")
    for f in failures[:30]:
        print("  - " + f)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
