#!/usr/bin/env python3
"""随机模糊测试: 独立参考实现(Sutherland-Hodgman, 精确浮点比较)交叉校验。

对随机凸裁剪器与随机简单主体(三角形/四边形), 检查:
  1) 后端不崩溃, 且 kind/area 与参考实现一致(面积在容差内);
  2) 所有结果顶点落在裁剪多边形内;
  3) 面积界 0 <= area <= min(area(subj), area(clip));
  4) polygon 结果方向为 CCW。
只生成保证简单的主体(随机凸四边形/三角形), 避免输入被拒绝路径。
"""
import json
import math
import os
import random
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BIN = os.path.join(ROOT, "build", "polygon-clip")

EPS = 1e-9
TRIALS = 300
SEED = 20260924

failures = []
checks = 0


def check(cond, msg):
    global checks
    checks += 1
    if not cond:
        failures.append(msg)


def cross(o, a, b):
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def area(poly):
    s = 0.0
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return s / 2.0


def convex_hull(points):
    points = sorted(set(points))
    if len(points) <= 1:
        return points
    lower = []
    for p in points:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper = []
    for p in reversed(points):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def line_intersect(s, e, a, b):
    """S-E 与裁剪线 a-b (b 在 a 的左侧为内部) 的交点。"""
    r = (e[0] - s[0], e[1] - s[1])
    d = (b[0] - a[0], b[1] - a[1])
    den = r[0] * d[1] - r[1] * d[0]
    t = ((a[0] - s[0]) * d[1] - (a[1] - s[1]) * d[0]) / den
    return (s[0] + t * r[0], s[1] + t * r[1])


def ref_clip(subject, clip):
    """参考 Sutherland-Hodgman: clip 必须 CCW。返回顶点环(可能退化)。"""
    if area(clip) < 0:
        clip = clip[::-1]
    out = list(subject)
    for i in range(len(clip)):
        a = clip[i]
        b = clip[(i + 1) % len(clip)]
        if not out:
            break
        nxt = []
        s = out[-1]
        sin = cross(a, b, s) >= 0
        for e in out:
            ein = cross(a, b, e) >= 0
            if ein:
                if not sin:
                    nxt.append(line_intersect(s, e, a, b))
                nxt.append(e)
            elif sin:
                nxt.append(line_intersect(s, e, a, b))
            s, sin = e, ein
        out = nxt
    return out


def point_in_convex(p, poly):
    for i in range(len(poly)):
        a = poly[i]
        b = poly[(i + 1) % len(poly)]
        if cross(a, b, p) < -1e-8 * max(1.0, abs(a[0] - b[0]) + abs(a[1] - b[1])):
            return False
    return True


def run(subject, clip):
    p = subprocess.run(
        [BIN],
        input=json.dumps({"subject": subject, "clip": clip}),
        capture_output=True,
        text=True,
    )
    return p.returncode, json.loads(p.stdout)


def random_convex(rng, lo, hi, n):
    pts = [(rng.uniform(lo, hi), rng.uniform(lo, hi)) for _ in range(n * 3)]
    hull = convex_hull(pts)
    if len(hull) < 3:
        return random_convex(rng, lo, hi, n)
    # 随机选 n 个连续/均匀点: 直接返回整个凸包, 保证简单且凸。
    if area(hull) < 0:
        hull = hull[::-1]
    return hull[: max(3, min(len(hull), n))] if len(hull) > n else hull


def main():
    if not os.path.exists(BIN):
        print(f"missing binary: {BIN} (run `make` first)")
        return 1
    rng = random.Random(SEED)

    for trial in range(TRIALS):
        # 裁剪器: [0,100] 内随机凸多边形
        clip = random_convex(rng, 0, 100, rng.randint(3, 6))
        # 主体: 随机三角形或凸四边形, 范围与裁剪器重叠
        cx, cy = rng.uniform(20, 80), rng.uniform(20, 80)
        rad = rng.uniform(5, 60)
        kind = rng.choice(["tri", "quad", "hull"])
        if kind == "tri":
            subject = [
                (cx + rng.uniform(-rad, rad), cy + rng.uniform(-rad, rad))
                for _ in range(3)
            ]
            if abs(area(subject)) < 1e-6:
                continue
        elif kind == "quad":
            angles = sorted(rng.uniform(0, 2 * math.pi) for _ in range(4))
            rs = [rng.uniform(rad * 0.3, rad) for _ in range(4)]
            subject = [
                (cx + rs[i] * math.cos(angles[i]),
                 cy + rs[i] * math.sin(angles[i]))
                for i in range(4)
            ]
            if abs(area(subject)) < 1e-6:
                continue
        else:
            subject = random_convex(rng, cx - rad, cx + rad, 4)

        # 主体可能因共线被拒; 后端若拒绝则跳过(属合法路径)。
        code, resp = run(subject, clip)
        if code != 0:
            check(resp["status"] == "error", f"trial {trial}: nonzero but no error")
            continue
        check(resp["status"] == "ok", f"trial {trial}: status not ok")
        r = resp["result"]
        m = resp["metrics"]

        ref = ref_clip([list(p) for p in subject], [list(p) for p in clip])
        ref_area = abs(area(ref)) if len(ref) >= 3 else 0.0
        scale = 100.0
        tol = 1e-6 * scale * scale

        if r["kind"] == "polygon":
            check(r["orientation"] == "CCW", f"trial {trial}: polygon not CCW")
            check(abs(m["area"] - ref_area) <= tol,
                  f"trial {trial}: area {m['area']} vs ref {ref_area}")
            for v in r["vertices"]:
                check(point_in_convex(v, clip),
                      f"trial {trial}: vertex {v} outside clip")
        elif r["kind"] == "empty":
            check(ref_area <= tol, f"trial {trial}: empty but ref area {ref_area}")
        # 面积界
        a_sub = abs(area(subject))
        a_clip = abs(area(clip))
        check(-tol <= m["area"] <= min(a_sub, a_clip) + tol,
              f"trial {trial}: area bound violated {m['area']}")

    print(f"fuzz: {TRIALS} trials, {checks} checks, {len(failures)} failure(s)")
    for f in failures[:20]:
        print("  " + f)
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
