#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""
精确线段相交参考实现（Python fractions.Fraction，纯有理数、无浮点）
+ 与 C++ segi-cli 的随机/退化对拍驱动。

对拍内容：
  1. relation 分类（disjoint / cross / endpoint_touch / collinear_overlap）
  2. 单点交点的有理坐标（num/den）
  3. contact_on_a / contact_on_b 端点来源标记
  4. 共线重叠区间 start/end 的有理坐标
  5. 零长标记
  6. 端点交换（p<->q）结果不变性

用法：python3 crosscheck.py --cli ./build/segi-cli --seed 20260923 --count 4000
"""
from __future__ import annotations

import argparse
import json
import random
import subprocess
import sys
from fractions import Fraction
from pathlib import Path


# ---------------- 参考实现（与 C++ 分类规则逐条对应） ----------------

def cross(ux, uy, vx, vy):
    return ux * vy - uy * vx


def dot(ux, uy, vx, vy):
    return ux * vx + uy * vy


def reference(A, B):
    (ax1, ay1), (ax2, ay2) = A
    (bx1, by1), (bx2, by2) = B
    az = A[0] == A[1]
    bz = B[0] == B[1]

    res = {"segment_a_zero_length": az, "segment_b_zero_length": bz}

    def point(x, y):
        return {"x": frac_json(x), "y": frac_json(y)}

    if az or bz:
        if az and bz:
            if A[0] == B[0]:
                res["relation"] = "endpoint_touch"
                res["point"] = point(Fraction(A[0][0]), Fraction(A[0][1]))
                res["contact_on_a"] = "p"
                res["contact_on_b"] = "p"
            else:
                res["relation"] = "disjoint"
            return res
        cx, cy = A[0] if az else B[0]
        (lx, ly), (mx, my) = (B if az else A)
        vx, vy = mx - lx, my - ly
        wx, wy = cx - lx, cy - ly
        on = cross(vx, vy, wx, wy) == 0 and \
             dot(wx, wy, vx, vy) >= 0 and \
             dot(wx, wy, vx, vy) <= dot(vx, vy, vx, vy)
        if not on:
            res["relation"] = "disjoint"
            return res
        res["relation"] = "endpoint_touch"
        res["point"] = point(Fraction(cx), Fraction(cy))
        if az:
            res["contact_on_a"] = "p"
            res["contact_on_b"] = "i" if 0 < dot(wx, wy, vx, vy) < dot(vx, vy, vx, vy) \
                else ("p" if (cx, cy) == (lx, ly) else "q")
        else:
            res["contact_on_b"] = "p"
            res["contact_on_a"] = "i" if 0 < dot(wx, wy, vx, vy) < dot(vx, vy, vx, vy) \
                else ("p" if (cx, cy) == (lx, ly) else "q")
        return res

    ux, uy = ax2 - ax1, ay2 - ay1
    vx, vy = bx2 - bx1, by2 - by1
    wx, wy = bx1 - ax1, by1 - ay1
    D = cross(ux, uy, vx, vy)
    sN = cross(wx, wy, vx, vy)
    tN = cross(wx, wy, ux, uy)

    def role(num):
        # 返回 -1/0/1/2：区间外/内部/端点/区间外（>1）
        nn = -num if D < 0 else num
        den = abs(D)
        if nn == 0 or nn == den:
            return 1
        if nn < 0:
            return -1
        return 0 if nn < den else 2

    if D != 0:
        ss, tt = role(sN), role(tN)
        if ss < 0 or ss > 1 or tt < 0 or tt > 1:
            res["relation"] = "disjoint"
            return res
        s = Fraction(sN, D)
        X = Fraction(ax1) + s * ux
        Y = Fraction(ay1) + s * uy
        res["point"] = point(X, Y)
        if ss == 0 and tt == 0:
            res["relation"] = "cross"
        else:
            res["relation"] = "endpoint_touch"
            res["contact_on_a"] = "i" if ss == 0 else ("p" if sN == 0 else "q")
            res["contact_on_b"] = "i" if tt == 0 else ("p" if tN == 0 else "q")
        return res

    # 平行
    if cross(wx, wy, ux, uy) != 0:
        res["relation"] = "disjoint"
        return res

    # 共线，参数 k = dot(P-A.p, u)/u2
    u2 = dot(ux, uy, ux, uy)

    def proj(P):
        return dot(P[0] - ax1, P[1] - ay1, ux, uy)

    kq, kr, ks = u2, proj(B[0]), proj(B[1])
    kp = 0
    lo, hi = kr, ks
    lo_is_bp = True
    if lo > hi:
        lo, hi = hi, lo
        lo_is_bp = False
    if hi < kp or lo > kq:
        res["relation"] = "disjoint"
        return res

    istart = max(kp, lo)
    iend = min(kq, hi)

    def at(k):
        x = Fraction(ax1 * u2 + k * ux, u2)
        y = Fraction(ay1 * u2 + k * uy, u2)
        return (x, y)

    if istart == iend:
        res["relation"] = "endpoint_touch"
        x, y = at(istart)
        res["point"] = point(x, y)
        res["contact_on_a"] = "p" if istart == kp else ("q" if istart == kq else "i")
        if istart == lo:
            res["contact_on_b"] = "p" if lo_is_bp else "q"
        elif istart == hi:
            res["contact_on_b"] = "q" if lo_is_bp else "p"
        else:
            res["contact_on_b"] = "i"
        return res

    sp, ep = at(istart), at(iend)
    if sp > ep:
        sp, ep = ep, sp
    res["relation"] = "collinear_overlap"
    res["overlap"] = {"start": point(*sp), "end": point(*ep)}
    return res


def frac_json(f: Fraction):
    # 输出任意精度 Python int；json.dumps 原样写十进制文本，与 C++ 的 JSON 数字逐字可比
    return {"num": f.numerator, "den": f.denominator}


# ---------------- 用例生成 ----------------

def small_point(rng, bound=12):
    return (rng.randint(-bound, bound), rng.randint(-bound, bound))


def gen_random(rng):
    return ([small_point(rng), small_point(rng)],
            [small_point(rng), small_point(rng)])


def gen_collinear(rng):
    # 沿某方向取四个共线参数，构造常见共线构型
    p = small_point(rng, 8)
    dx, dy = rng.choice([(1, 0), (0, 1), (1, 1), (2, 1), (1, -2), (-1, 1)])
    ts = sorted(rng.sample(range(-6, 7), 4))
    a = [(p[0] + ts[0] * dx, p[1] + ts[0] * dy),
         (p[0] + ts[2] * dx, p[1] + ts[2] * dy)]
    b = [(p[0] + ts[1] * dx, p[1] + ts[1] * dy),
         (p[0] + ts[3] * dx, p[1] + ts[3] * dy)]
    if rng.random() < 0.5:
        a[0], a[1] = a[1], a[0]
    return a, b


def gen_zero(rng):
    a = [small_point(rng), small_point(rng)]
    b = [small_point(rng), small_point(rng)]
    if rng.random() < 0.7:
        a[1] = a[0]
    if rng.random() < 0.7:
        b[1] = b[0]
    # 一定概率把退化点放到另一线段的某个端点上
    if a[0] == a[1] and b[0] != b[1] and rng.random() < 0.5:
        pick = rng.choice([0, 1])
        a[0] = a[1] = b[pick]
    return a, b


def gen_huge(rng):
    B = 2 ** 62
    p = lambda: (rng.randint(-B, B), rng.randint(-B, B))
    return ([p(), p()], [p(), p()])


def gen_oversize(rng):
    B = 10 ** 40
    p = lambda: (rng.randint(-B, B), rng.randint(-B, B))
    return ([p(), p()], [p(), p()])


# ---------------- 对拍 ----------------

def seg_json(s):
    return {"p": {"x": s[0][0], "y": s[0][1]},
            "q": {"x": s[1][0], "y": s[1][1]}}


def swap(s):
    return [s[1], s[0]]


def normalize_result(r):
    # C++ 输出的 num/den 是十进制字符串；Python 也用字符串构造，直接比较即可
    return json.dumps(r, sort_keys=True)


def run_batch(cli, queries):
    req = json.dumps({"queries": [{"a": seg_json(a), "b": seg_json(b)}
                                  for a, b in queries]})
    proc = subprocess.run([cli], input=req, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"CLI failed rc={proc.returncode}\nstderr={proc.stderr}\nreq={req[:500]}")
    return json.loads(proc.stdout)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cli", default="./build/segi-cli")
    ap.add_argument("--seed", type=int, default=20260923)
    ap.add_argument("--count", type=int, default=4000)
    ap.add_argument("--batch-size", type=int, default=200)
    args = ap.parse_args()

    if not Path(args.cli).exists():
        sys.exit(f"CLI not found: {args.cli} (build with: make)")

    rng = random.Random(args.seed)
    cases = []
    # 分布：随机 + 强制共线 + 零长 + 大坐标 + 超 64 位
    weights = [("random", gen_random, 0.55),
               ("collinear", gen_collinear, 0.20),
               ("zero", gen_zero, 0.15),
               ("huge", gen_huge, 0.05),
               ("oversize", gen_oversize, 0.05)]
    kind_stats = {}
    for i in range(args.count):
        r = rng.random()
        acc = 0
        for name, fn, w in weights:
            acc += w
            if r <= acc:
                a, b = fn(rng)
                kind_stats[name] = kind_stats.get(name, 0) + 1
                break
        else:
            a, b = gen_random(rng)
            kind_stats["random"] = kind_stats.get("random", 0) + 1
        cases.append((name, a, b))

    # 加入结构性固定用例
    fixed = [
        ("fixed", [(0, 0), (2, 2)], [(0, 2), (2, 0)]),
        ("fixed", [(0, 0), (0, 0)], [(0, 0), (4, 0)]),
        ("fixed", [(0, 0), (4, 0)], [(2, 0), (2, 0)]),
        ("fixed", [(0, 0), (2, 0)], [(2, 0), (4, 0)]),
        ("fixed", [(0, 0), (2, 0)], [(3, 0), (5, 0)]),
        ("fixed", [(0, 0), (3, 1)], [(0, 2), (2, 0)]),
        ("fixed", [(-2 ** 63 + 1, 0), (2 ** 63 - 1, 0)], [(0, 0), (0, 1)]),
    ]
    for name, a, b in fixed:
        cases.append((name, a, b))

    # 每个用例扩展为 4 种端点排列
    expanded = []
    for name, a, b in cases:
        expanded.append((name, a, b, False, False))
        expanded.append((name, swap(a), b, True, False))
        expanded.append((name, a, swap(b), False, True))
        expanded.append((name, swap(a), swap(b), True, True))

    failures = []
    rel_stats = {}
    total = len(expanded)

    for off in range(0, total, args.batch_size):
        chunk = expanded[off:off + args.batch_size]
        resp = run_batch(args.cli, [(a, b) for _, a, b, _, _ in chunk])
        results = resp["results"]
        for (name, a, b, sa, sb), item in zip(chunk, results):
            if "error" in item:
                failures.append((name, a, b, f"CLI error: {item['error']}", None, None))
                continue
            got = item["result"]
            want = reference(a, b)
            rel_stats[got["relation"]] = rel_stats.get(got["relation"], 0) + 1
            if normalize_result(got) != normalize_result(want):
                failures.append((name, a, b, "mismatch", got, want))
                if len(failures) >= 10:
                    break
        if len(failures) >= 10:
            break

    print(f"crosscheck: {total} comparisons ({len(cases)} cases x 4 endpoint orderings)")
    print(f"  case kinds (base): {kind_stats}")
    print(f"  relation tally   : {rel_stats}")
    if failures:
        print(f"\nFAILED ({len(failures)} shown, up to 10):")
        for name, a, b, why, got, want in failures:
            print(f"  [{name}] {why}\n    A={a}\n    B={b}")
            if got is not None:
                print(f"    got ={json.dumps(got, sort_keys=True)}")
                print(f"    want={json.dumps(want, sort_keys=True)}")
        sys.exit(1)
    print("crosscheck OK: C++ output matches Fraction reference on every case")


if __name__ == "__main__":
    main()
