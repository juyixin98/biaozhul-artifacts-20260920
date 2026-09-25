#!/usr/bin/env python3
"""随机对拍：C++ 后端 vs 独立的 Python 有理数参考实现。

参考实现使用 fractions.Fraction 做精确运算，判定逻辑独立编写
（参数法 s,t in [0,1] + 共线一维区间），不参考 C++ 源码结构。

用法:
  python3 random_compare.py --binary ./seginter --count 20000 \
      --coord-range 8 --seed 20260923
"""

import argparse
import json
import random
import subprocess
import sys
from fractions import Fraction

NAME = {0: "a", 1: "b", 2: "c", 3: "d"}


def cross(u, v):
    return u[0] * v[1] - u[1] * v[0]


def sub(p, q):
    return (p[0] - q[0], p[1] - q[1])


def point_on_segment(p, a, b):
    """点 p 是否落在（可退化的）线段 ab 上，精确整数判定。"""
    if cross(sub(b, a), sub(p, a)) != 0:
        return False
    return (min(a[0], b[0]) <= p[0] <= max(a[0], b[0])
            and min(a[1], b[1]) <= p[1] <= max(a[1], b[1]))


def hits_at(p, a, b, c, d):
    pts = (a, b, c, d)
    return sorted(NAME[i] for i in range(4) if pts[i] == p)


def reference(a, b, c, d):
    """返回 (classification, points)；points 为 (x, y, hits) 列表。

    x/y 为 Fraction；overlap 的点按在线上的顺序排列。
    """
    ab_deg = a == b
    cd_deg = c == d

    # ---- 退化 ----
    if ab_deg and cd_deg:
        return ("touch", [(a[0], a[1], hits_at(a, a, b, c, d))]) if a == c \
            else ("none", [])
    if ab_deg:
        if point_on_segment(a, c, d):
            return ("touch", [(a[0], a[1], hits_at(a, a, b, c, d))])
        return ("none", [])
    if cd_deg:
        if point_on_segment(c, a, b):
            return ("touch", [(c[0], c[1], hits_at(c, a, b, c, d))])
        return ("none", [])

    # ---- 非退化：参数法 ----
    u = sub(b, a)
    v = sub(d, c)
    den = cross(u, v)

    if den != 0:
        w = sub(c, a)
        s = Fraction(cross(w, v), den)   # a + s*u
        t = Fraction(cross(w, u), den)   # c + t*v
        if not (0 <= s <= 1 and 0 <= t <= 1):
            return ("none", [])
        px = Fraction(a[0]) + s * u[0]
        py = Fraction(a[1]) + s * u[1]
        if 0 < s < 1 and 0 < t < 1:
            return ("cross", [(px, py, [])])
        hits = []
        if s == 0: hits.append("a")
        if s == 1: hits.append("b")
        if t == 0: hits.append("c")
        if t == 1: hits.append("d")
        return ("touch", [(px, py, sorted(hits))])

    # 平行：共线？
    if cross(u, sub(c, a)) != 0:
        return ("none", [])

    # 共线：以 u 做点积投影（严格递增的整数标量）
    L = u[0] * u[0] + u[1] * u[1]  # > 0

    def r(p):
        w = sub(p, a)
        return w[0] * u[0] + w[1] * u[1]

    rc, rd = r(c), r(d)
    lo = max(0, min(rc, rd))
    hi = min(L, max(rc, rd))
    if lo > hi:
        return ("none", [])

    def point_at(value):
        # value 是某端点的投影值时，直接取对应的整数端点
        for idx, p in ((0, a), (1, b), (2, c), (3, d)):
            if r(p) == value:
                return (p[0], p[1], hits_at(p, a, b, c, d))
        raise AssertionError("interval endpoint must be a segment endpoint")

    if lo == hi:
        return ("touch", [point_at(lo)])
    return ("overlap", [point_at(lo), point_at(hi)])


def entry(idx, a, b, c, d):
    return {
        "id": idx,
        "a": {"x": a[0], "y": a[1]},
        "b": {"x": b[0], "y": b[1]},
        "c": {"x": c[0], "y": c[1]},
        "d": {"x": d[0], "y": d[1]},
    }


def parse_frac(o):
    return Fraction(int(o["num"]), int(o["den"]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--binary", default="./seginter")
    ap.add_argument("--count", type=int, default=20000)
    ap.add_argument("--coord-range", type=int, default=8)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--batch-size", type=int, default=2000)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    R = args.coord_range

    def rpt():
        return (rng.randint(-R, R), rng.randint(-R, R))

    # (a,b,c,d, 期望) 队列；每条用例同时测 4 种端点交换
    cases = []

    # 注入一批确定性退化/边界用例
    crafted = [
        ((0, 0), (0, 0), (0, 0), (0, 0)),
        ((1, 1), (1, 1), (2, 2), (2, 2)),
        ((3, 0), (3, 0), (0, 0), (6, 0)),
        ((0, 0), (4, 4), (2, 2), (2, 2)),
        ((R, R), (R, R), (R, R), (R, R)),
        ((-R, 0), (R, 0), (0, 0), (0, 0)),
    ]
    for cs in crafted:
        cases.append(cs)

    for _ in range(args.count):
        cases.append((rpt(), rpt(), rpt(), rpt()))

    # 每条原始用例展开为：原样、交换 ab、交换 cd、线段对互换
    variants = []
    for (a, b, c, d) in cases:
        variants.append((a, b, c, d))
        variants.append((b, a, c, d))
        variants.append((a, b, d, c))
        variants.append((c, d, a, b))

    total = 0
    mismatches = 0

    for start in range(0, len(variants), args.batch_size):
        chunk = variants[start:start + args.batch_size]
        entries = [entry(i, *cs) for i, cs in enumerate(chunk)]
        proc = subprocess.run(
            [args.binary], input=json.dumps({"entries": entries}),
            capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            print("binary failed:", proc.stderr)
            return 2
        resp = json.loads(proc.stdout)
        results = resp["results"]
        if len(results) != len(chunk):
            print("result count mismatch")
            return 2

        for got, (a, b, c, d) in zip(results, chunk):
            total += 1
            exp_cls, exp_pts = reference(a, b, c, d)
            if "error" in got:
                print("UNEXPECTED ERROR", (a, b, c, d), got["error"])
                mismatches += 1
                continue
            if got["classification"] != exp_cls:
                print("CLASS MISMATCH", (a, b, c, d),
                      "got", got["classification"], "expected", exp_cls)
                mismatches += 1
                continue
            got_pts = got["points"]
            if len(got_pts) != len(exp_pts):
                print("POINT COUNT MISMATCH", (a, b, c, d), got_pts, exp_pts)
                mismatches += 1
                continue
            # 点的输出顺序规范为坐标升序（与端点方向无关），比较前统一排序。
            got_norm = sorted(
                ((parse_frac(gp["x"]), parse_frac(gp["y"]),
                  tuple(sorted(gp["hits"]))) for gp in got_pts),
                key=lambda t: (t[0], t[1]))
            exp_norm = sorted(
                ((ex, ey, tuple(eh)) for (ex, ey, eh) in exp_pts),
                key=lambda t: (t[0], t[1]))
            for (gx, gy, gh), (ex, ey, eh) in zip(got_norm, exp_norm):
                if (gx, gy) != (ex, ey) or gh != eh:
                    print("POINT MISMATCH", (a, b, c, d),
                          "got", (str(gx), str(gy), list(gh)),
                          "expected", (str(ex), str(ey), list(eh)))
                    mismatches += 1

    print(f"cases compared: {total}, mismatches: {mismatches}")
    if mismatches:
        print("RANDOM COMPARISON FAILED")
        return 1
    print("RANDOM COMPARISON PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
