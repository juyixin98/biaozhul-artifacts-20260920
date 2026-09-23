#!/usr/bin/env python3
"""Differential test: compare the pointloc binary (both 'naive' and
'indexed' engines) against an independent exact-integer reference
implementation in pure Python.

Cases cover the required degeneracies:
  * horizontal ray through a vertex (local min/max, left/right tips);
  * points exactly on a hole edge and on the outer edge;
  * rings supplied in either orientation (CW and CCW);
  * large coordinates (products up to ~1e37, still exact in the C++ engine);
  * randomized concave (star) polygons with multiple holes.

Usage: differential_test.py /path/to/pointloc
"""

import json
import math
import os
import random
import subprocess
import sys

LOC = {-1: "outside", 0: "boundary", 1: "inside"}


# ---------------------------------------------------------------- reference
def orient(a, b, p):
    return (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])


def on_segment(p, a, b):
    if orient(a, b, p) != 0:
        return False
    return (min(a[0], b[0]) <= p[0] <= max(a[0], b[0]) and
            min(a[1], b[1]) <= p[1] <= max(a[1], b[1]))


def ref_ring(poly_ring, p):
    """Exact half-open-edge ray casting, boundary first."""
    n = len(poly_ring)
    for i in range(n):
        if on_segment(p, poly_ring[i], poly_ring[(i + 1) % n]):
            return 0
    crossings = 0
    for i in range(n):
        ax, ay = poly_ring[i]
        bx, by = poly_ring[(i + 1) % n]
        if not ((ay <= p[1] < by) or (by <= p[1] < ay)):
            continue
        if orient(poly_ring[i], poly_ring[(i + 1) % n], p) == 0:
            continue
        # xint > p.x, direction-independent compare.
        dx, dy = bx - ax, by - ay
        z, w = p[1] - ay, p[0] - ax
        positive = dx * z > dy * w
        if (dy > 0) == positive:
            crossings += 1
    return 1 if crossings % 2 else -1


def ref_locate(polygon, p):
    outer, holes = polygon
    r = ref_ring(outer, p)
    if r != 1:
        return r
    for h in holes:
        hr = ref_ring(h, p)
        if hr == 0:
            return 0
        if hr == 1:
            return -1
    return 1


def valid_simple(ring):
    """Independently check simple-ring assumptions for generated data."""
    n = len(ring)
    if n < 3:
        return False
    for i in range(n):
        if ring[i] == ring[(i + 1) % n]:
            return False
    area2 = 0
    for i in range(n):
        area2 += (ring[(i + 1) % n][0] - ring[i][0]) * \
                 (ring[(i + 1) % n][1] + ring[i][1])
    if area2 == 0:
        return False
    for i in range(n):
        a, b = ring[i], ring[(i + 1) % n]
        for j in range(i + 1, n):
            if j == i + 1 or (i == 0 and j == n - 1):
                continue
            c, d = ring[j], ring[(j + 1) % n]
            # proper crossing or any endpoint-on-segment
            s1, s2 = orient(a, b, c), orient(a, b, d)
            s3, s4 = orient(c, d, a), orient(c, d, b)
            if ((s1 > 0 and s2 < 0) or (s1 < 0 and s2 > 0)) and \
               ((s3 > 0 and s4 < 0) or (s3 < 0 and s4 > 0)):
                return False
            if any(on_segment(q, c, d) for q in (a, b)):
                return False
            if any(on_segment(q, a, b) for q in (c, d)):
                return False
    return True


def reverse_ring(ring):
    return [ring[0]] + list(reversed(ring[1:]))


# ------------------------------------------------------------- generators
def star_ring(rng, n, cx, cy, rmin, rmax, scale=1):
    while True:
        ring = []
        for i in range(n):
            ang = 2 * math.pi * i / n
            r = rng.randint(rmin, rmax)
            ring.append((cx + int(r * math.cos(ang)),
                         cy + int(r * math.sin(ang))))
        if valid_simple(ring):
            return ring


def contained_star(rng, n, outer, cx, cy, radius, other_holes=()):
    """Star fully inside outer and disjoint (no touch/nest) from others."""
    def disjoint(h1, h2):
        m1, m2 = len(h1), len(h2)
        for i in range(m1):
            a, b = h1[i], h1[(i + 1) % m1]
            for j in range(m2):
                c, d = h2[j], h2[(j + 1) % m2]
                s1, s2 = orient(a, b, c), orient(a, b, d)
                s3, s4 = orient(c, d, a), orient(c, d, b)
                if ((s1 > 0 and s2 < 0) or (s1 < 0 and s2 > 0)) and \
                   ((s3 > 0 and s4 < 0) or (s3 < 0 and s4 > 0)):
                    return False
                if (on_segment(a, c, d) or on_segment(b, c, d) or
                        on_segment(c, a, b) or on_segment(d, a, b)):
                    return False
        # Nested iff a vertex of one lies inside the other.
        if ref_ring(h1, h2[0]) != -1 or ref_ring(h2, h1[0]) != -1:
            return False
        return True

    while True:
        ring = star_ring(rng, n, cx, cy, radius // 2, radius)
        if not all(ref_ring(outer, v) == 1 for v in ring):
            continue
        if all(disjoint(ring, h) for h in other_holes):
            return ring


def edge_midpoint(a, b):
    # Integer point on the edge (not necessarily its geometric midpoint).
    return (a[0] + b[0]) // 2, (a[1] + b[1]) // 2


# ----------------------------------------------------------------- driver
class Runner:
    def __init__(self, binary):
        self.binary = binary
        self.cases = 0
        self.mismatches = []

    def run_case(self, polygon, queries, tag, expect_error=None):
        outer, holes = polygon
        payload = {"polygon": {"outer": [{"x": x, "y": y} for x, y in outer],
                               "holes": [[{"x": x, "y": y} for x, y in h]
                                         for h in holes]},
                   "queries": [{"x": x, "y": y} for x, y in queries]}
        for method in ("naive", "indexed"):
            payload["method"] = method
            proc = subprocess.run([self.binary], input=json.dumps(payload),
                                  capture_output=True, text=True)
            if expect_error is not None:
                if proc.returncode == 0:
                    self.mismatches.append(
                        f"[{tag}/{method}] expected error {expect_error}, "
                        f"got rc=0")
                elif expect_error not in proc.stdout:
                    self.mismatches.append(
                        f"[{tag}/{method}] expected error string "
                        f"'{expect_error}', got: {proc.stdout.strip()}")
                continue
            if proc.returncode != 0:
                self.mismatches.append(
                    f"[{tag}/{method}] rc={proc.returncode}: "
                    f"{proc.stdout.strip()} {proc.stderr.strip()}")
                continue
            out = json.loads(proc.stdout)
            if out.get("status") != "ok":
                self.mismatches.append(f"[{tag}/{method}] status!=ok: {out}")
                continue
            results = out["results"]
            if len(results) != len(queries):
                self.mismatches.append(
                    f"[{tag}/{method}] result count {len(results)} != "
                    f"{len(queries)}")
                continue
            for q, item in zip(queries, results):
                self.cases += 1
                want = LOC[ref_locate(polygon, q)]
                got = item["location"]
                if got != want:
                    self.mismatches.append(
                        f"[{tag}/{method}] q={q} got={got} want={want}")

    def report(self):
        print(f"{self.cases} point classifications across both engines, "
              f"{len(self.mismatches)} mismatches")
        for m in self.mismatches[:20]:
            print("  " + m)
        return len(self.mismatches) == 0


def main():
    binary = sys.argv[1] if len(sys.argv) > 1 else "./pointloc"
    if not os.path.exists(binary):
        print(f"binary not found: {binary}", file=sys.stderr)
        return 2

    rng = random.Random(20260924)
    run = Runner(binary)

    # 1) Fixed square with a square hole, including boundary degeneracies.
    sq = [(0, 0), (10, 0), (10, 10), (0, 10)]
    hole = [(3, 3), (7, 3), (7, 7), (3, 7)]
    queries = [
        (5, 5), (-1, 5), (5, -1), (11, 5), (5, 11),
        (0, 5), (10, 5), (5, 0), (5, 10),          # outer edges
        (5, 3), (7, 5), (5, 7), (3, 5),            # hole edges
        (3, 3), (7, 7),                            # hole vertices
        (1, 1), (9, 9), (2, 5), (8, 5),
        (-5, 0), (-5, 10),                         # ray along edge line
    ]
    run.run_case((sq, [hole]), queries, "square+hole")
    run.run_case((reverse_ring(sq), [reverse_ring(hole)]), queries,
                 "square+hole (CW)")

    # Diamond whose left/right tips sit on query y-values.
    dia = [(0, -10), (10, 0), (0, 10), (-10, 0)]
    dq = [(-20, 0), (-10, 0), (0, 0), (10, 0), (20, 0),
          (-5, 10), (0, 10), (5, 10), (0, -10), (-5, -10), (5, -10)]
    run.run_case((dia, []), dq, "diamond")
    run.run_case((reverse_ring(dia), []), dq, "diamond (CW)")

    # 2) Large coordinates (signed 128-bit products, exact).
    B = 4_000_000_000_000_000_000
    big = [(-B, -B), (B, -B), (B, B), (-B, B)]
    bighole = [(-B // 4, -B // 4), (B // 4, -B // 4),
               (B // 4, B // 4), (-B // 4, B // 4)]
    bq = [(0, 0), (B // 2, B // 2), (B, 0), (-B // 4, 0),
          (B + 1, 0), (0, B + 1), (B - 1, 1), (-B, -B),
          (B // 8, 0)]
    run.run_case((big, [bighole]), bq, "large coords")
    run.run_case((reverse_ring(big), [reverse_ring(bighole)]), bq,
                 "large coords (CW)")

    # 3) Randomized concave stars with holes; many random queries plus
    #    forced vertex-y and edge queries (ray-through-vertex coverage).
    for trial in range(12):
        outer = star_ring(rng, rng.choice([9, 13, 19, 27]), 0, 0,
                          800, 1000)
        holes = []
        for _ in range(rng.randint(0, 3)):
            ang = rng.uniform(0, 2 * math.pi)
            d = rng.randint(150, 450)
            cx, cy = int(d * math.cos(ang)), int(d * math.sin(ang))
            holes.append(contained_star(
                rng, rng.choice([6, 8, 10]), outer, cx, cy,
                rng.randint(40, 70), other_holes=holes))
        qs = [(rng.randint(-1200, 1200), rng.randint(-1200, 1200))
              for _ in range(80)]
        # vertex-y queries
        ys = [v[1] for v in outer]
        for h in holes:
            ys += [v[1] for v in h]
        for y in ys:
            qs.append((rng.randint(-1200, 1200), y))
        # edge points (boundary coverage for outer and holes)
        for ring in [outer] + holes:
            for _ in range(6):
                i = rng.randrange(len(ring))
                qs.append(edge_midpoint(ring[i], ring[(i + 1) % len(ring)]))
        run.run_case((outer, holes), qs, f"star#{trial}")
        run.run_case((reverse_ring(outer), [reverse_ring(h) for h in holes]),
                     qs, f"star#{trial} (CW)")

    # 4) Invalid geometry must be rejected.
    bowtie = [(0, 0), (10, 10), (10, 0), (0, 10)]
    run.run_case((bowtie, []), [(5, 5)], "bowtie",
                 expect_error="self_intersection")
    run.run_case((sq, [[(30, 30), (40, 30), (40, 40), (30, 40)]]),
                 [(35, 35)], "hole outside",
                 expect_error="hole_outside_outer")

    ok = run.report()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
