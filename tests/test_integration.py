#!/usr/bin/env python3
"""集成测试：驱动 polygon_clip CLI，校验 JSON 契约与几何不变量。

两类测试：
1. 确定性用例（手算案例、全包含、无交、沿边重合、退化点/段、自交拒绝）。
2. 随机用例：生成凸裁剪域（凸包）与简单星形被裁多边形，用独立的栅格采样
   估计交集真实面积，与程序输出面积对比；并校验所有结果点在裁剪闭区域内、
   输出不自交、CCW。

退出码：0 全部通过，非零有失败。仅用 Python 标准库。
"""
import json
import math
import os
import random
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "build", "polygon_clip")

FAILS = []
CHECKS = 0


def check(cond, msg):
    global CHECKS
    CHECKS += 1
    if not cond:
        FAILS.append(msg)
        print(f"  [FAIL] {msg}")


def run(subject, clip):
    proc = subprocess.run(
        [BIN],
        input=json.dumps({"subject": subject, "clip": clip}),
        capture_output=True,
        text=True,
    )
    return proc.returncode, json.loads(proc.stdout)


def ring_area(r):
    return abs(sum(r[i][0] * r[(i + 1) % len(r)][1] -
                   r[(i + 1) % len(r)][0] * r[i][1]
                   for i in range(len(r))) / 2.0)


def signed_area(r):
    return sum(r[i][0] * r[(i + 1) % len(r)][1] -
               r[(i + 1) % len(r)][0] * r[i][1]
               for i in range(len(r))) / 2.0


def point_in_convex(p, ccw, tol=0.0):
    x, y = p
    n = len(ccw)
    for i in range(n):
        ax, ay = ccw[i]
        bx, by = ccw[(i + 1) % n]
        cross = (bx - ax) * (y - ay) - (by - ay) * (x - ax)
        if cross < -tol:
            return False
    return True


def point_in_simple(p, r, tol=0.0):
    x, y = p
    inside = False
    n = len(r)
    j = n - 1
    for i in range(n):
        ax, ay = r[j]
        bx, by = r[i]
        # 到线段距离（容差内含边界）
        dx, dy = bx - ax, by - ay
        L2 = dx * dx + dy * dy
        if L2 > 0:
            t = max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / L2))
            px, py = ax + t * dx, ay + t * dy
            if math.hypot(x - px, y - py) <= tol:
                return True
        if (ay > y) != (by > y):
            xc = ax + (bx - ax) * (y - ay) / (by - ay)
            if xc > x:
                inside = not inside
        j = i
    return inside


def segs_intersect(a, b, c, d):
    def cr(p, q, r):
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])
    d0 = cr(c, d, a)
    d1 = cr(c, d, b)
    d2 = cr(a, b, c)
    d3 = cr(a, b, d)
    return ((d0 > 0) != (d1 > 0)) and ((d2 > 0) != (d3 > 0))


def is_simple(r, eps=1e-9):
    n = len(r)
    for i in range(n):
        for j in range(i + 1, n):
            a, b = r[i], r[(i + 1) % n]
            c, d = r[j], r[(j + 1) % n]
            adj = ((i + 1) % n == j) or ((j + 1) % n == i)
            if adj:
                continue
            if segs_intersect(a, b, c, d):
                return False
    return True


# ---------------------------------------------------------------------------
# 1. 确定性用例
# ---------------------------------------------------------------------------
SQ = [[0, 0], [10, 0], [10, 10], [0, 10]]


def test_hand_triangle():
    print("[it] 手算案例（三角形 -> 面积50矩形）")
    rc, d = run([[-5, 5], [5, -5], [15, 5]], SQ)
    check(rc == 0 and d["ok"] is True, "返回成功")
    check(d["kind"] == "POLYGON", "kind=POLYGON")
    check(abs(d["area"] - 50.0) < 1e-7, f"面积=50, 实际 {d['area']}")
    check(d["orientation"] == "CCW" and d["signed_area"] > 0, "输出 CCW")
    check(d["vertex_count"] == 4, "4 个顶点")
    want = {(0, 0), (10, 0), (10, 5), (0, 5)}
    got = {(round(x, 8), round(y, 8)) for x, y in d["vertices"]}
    check(want == got, f"顶点匹配: 实际 {got}")
    for p in d["vertices"]:
        check(point_in_convex(p, SQ, 1e-8), "结果点在裁剪域内")
    check(d["coordinate_system"]["type"] == "planar_cartesian", "坐标系已声明")


def test_contained():
    print("[it] 全包含")
    rc, d = run([[2, 2], [8, 2], [8, 8], [2, 8]], SQ)
    check(d["kind"] == "POLYGON" and abs(d["area"] - 36) < 1e-7, "面积36")
    check(abs(ring_area(d["vertices"]) - 36) < 1e-7, "顶点环面积一致")


def test_no_intersection():
    print("[it] 无交集")
    rc, d = run([[-10, -10], [-9, -10], [-9, -9], [-10, -9]], SQ)
    check(rc == 0, "无交集仍为正常返回(rc=0)")
    check(d["kind"] == "EMPTY" and d["vertices"] == [], "EMPTY 且无顶点")
    check(d["area"] == 0, "面积0")


def test_edge_coincidence():
    print("[it] 沿边重合")
    rc, d = run([[2, -2], [8, -2], [8, 2], [2, 2]], SQ)
    check(d["kind"] == "POLYGON" and abs(d["area"] - 12) < 1e-7,
          f"面积12, 实际 {d['area']}")
    got = {(round(x, 8), round(y, 8)) for x, y in d["vertices"]}
    check(got == {(2, 0), (8, 0), (8, 2), (2, 2)}, "沿边顶点正确")


def test_segment():
    print("[it] 退化线段")
    rc, d = run([[-2, -2], [12, -2], [12, 0], [-2, 0]], SQ)
    check(d["kind"] == "SEGMENT", "SEGMENT")
    check(d["vertex_count"] == 2 and abs(d["area"]) < 1e-9, "两顶点面积0")
    got = {(round(x, 8), round(y, 8)) for x, y in d["vertices"]}
    check(got == {(0, 0), (10, 0)}, f"段端点 (0,0),(10,0), 实际 {got}")


def test_point():
    print("[it] 退化点")
    rc, d = run([[-5, -5], [5, -5], [0, 0]], SQ)
    check(d["kind"] == "POINT", "POINT")
    check(d["vertex_count"] == 1, "单点")
    x, y = d["vertices"][0]
    check(abs(x) < 1e-8 and abs(y) < 1e-8, f"点(0,0), 实际 ({x},{y})")


def test_self_intersect():
    print("[it] 自交拒绝")
    rc, d = run([[0, 0], [10, 10], [0, 10], [10, 0]], SQ)
    check(rc == 1 and d["ok"] is False, "rc=1 且 ok=false")
    check(d["status"] == "SELF_INTERSECTING", "SELF_INTERSECTING")


def test_bad_clip():
    print("[it] 非凸裁剪域拒绝")
    rc, d = run([[1, 1], [9, 1], [9, 9], [1, 9]],
                [[0, 0], [10, 0], [10, 10], [5, 5], [0, 10]])
    check(d["status"] == "INVALID_CLIP", "INVALID_CLIP")


def test_bad_json():
    print("[it] 非法 JSON")
    proc = subprocess.run([BIN], input="{not json", capture_output=True, text=True)
    d = json.loads(proc.stdout)
    check(d["status"] == "BAD_JSON", "BAD_JSON")


# ---------------------------------------------------------------------------
# 2. 随机用例 + 栅格独立面积验证
# ---------------------------------------------------------------------------
def convex_hull(pts):
    pts = sorted(pts)
    if len(pts) <= 1:
        return pts
    def cr(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
    lower = []
    for p in pts:
        while len(lower) >= 2 and cr(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper = []
    for p in reversed(pts):
        while len(upper) >= 2 and cr(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def raster_intersection_area(subj, clip, bbox, cells=120):
    """半格点采样：以采样点同时位于两多边形（含边界）的比例 * bbox 面积。"""
    x0, y0, x1, y1 = bbox
    inside = 0
    total = cells * cells
    for i in range(cells):
        sx = x0 + (i + 0.5) / cells * (x1 - x0)
        for j in range(cells):
            sy = y0 + (j + 0.5) / cells * (y1 - y0)
            if point_in_simple((sx, sy), subj, 1e-12) and \
               point_in_convex((sx, sy), clip, 1e-12):
                inside += 1
    return inside / total * (x1 - x0) * (y1 - y0)


def test_random():
    print("[it] 随机用例 x40（栅格独立验面积 + 包含性 + 不自交 + CCW）")
    rng = random.Random(20260924)
    for it in range(40):
        m = rng.randint(3, 7)
        raw = []
        for _ in range(m):
            a = rng.uniform(0, 2 * math.pi)
            rr = rng.uniform(4, 9)
            cx = rng.uniform(-4, 4)
            cy = rng.uniform(-4, 4)
            raw.append((cx + rr * math.cos(a), cy + rr * math.sin(a)))
        clip = convex_hull(raw)
        if len(clip) < 3:
            continue
        if signed_area(clip) < 0:
            clip.reverse()

        # 简单星形（绕中心角序、正半径）
        k = rng.randint(3, 8)
        cx = rng.uniform(-8, 8)
        cy = rng.uniform(-8, 8)
        subj = []
        for i in range(k):
            a = 2 * math.pi * i / k + rng.uniform(-0.05, 0.05)
            rr = rng.uniform(1, 9)
            subj.append([cx + rr * math.cos(a), cy + rr * math.sin(a)])
        if signed_area(subj) < 0:
            subj.reverse()

        rc, d = run(subj, clip)
        tag = f"case#{it}"
        check(rc == 0, f"{tag}: rc=0 ({d.get('message')})")

        xs = [p[0] for p in subj + clip]
        ys = [p[1] for p in subj + clip]
        bbox = (min(xs), min(ys), max(xs), max(ys))

        if d["kind"] == "POLYGON":
            out = d["vertices"]
            # 面积界
            a_subj = ring_area(subj)
            a_clip = ring_area(clip)
            check(d["area"] <= a_subj + 1e-6 * max(1, a_subj),
                  f"{tag}: 结果面积<=被裁面积 ({d['area']}>{a_subj})")
            check(d["area"] <= a_clip + 1e-6 * max(1, a_clip),
                  f"{tag}: 结果面积<=裁剪面积")
            # CCW
            check(signed_area(out) > 0, f"{tag}: 输出 CCW")
            # 不自交
            check(is_simple(out), f"{tag}: 输出环简单不自交")
            # 所有点在裁剪闭区域
            for p in out:
                check(point_in_convex(p, clip, 1e-7),
                      f"{tag}: 结果点 {p} 在裁剪域内")
            # 栅格独立面积。采样的绝对误差量级为“交集周长 × 格宽”，
            # 故判据按输出环周长缩放（对小面积区域放宽相对容差）。
            rast = raster_intersection_area(subj, clip, bbox)
            cell = ((bbox[2] - bbox[0]) + (bbox[3] - bbox[1])) / 240.0
            perim = sum(math.hypot(out[(i + 1) % len(out)][0] - out[i][0],
                                   out[(i + 1) % len(out)][1] - out[i][1])
                        for i in range(len(out)))
            allowed = max(0.03 * max(rast, d["area"]), perim * cell * 3.0)
            check(abs(rast - d["area"]) <= allowed,
                  f"{tag}: 栅格面积 {rast:.4f} vs 输出 {d['area']:.4f} "
                  f"(差 {abs(rast - d['area']):.4f}, 允许 {allowed:.4f})")
        elif d["kind"] in ("POINT", "SEGMENT"):
            check(abs(d["area"]) < 1e-7, f"{tag}: 退化面积为0")
            for p in d["vertices"]:
                check(point_in_convex(p, clip, 1e-7),
                      f"{tag}: 退化结果点在裁剪域内 {p}")
        else:
            # EMPTY：栅格面积应接近 0
            rast = raster_intersection_area(subj, clip, bbox, cells=60)
            bbox_area = (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])
            check(rast / bbox_area < 0.01, f"{tag}: EMPTY 但栅格有交集 {rast}")


def main():
    if not os.path.exists(BIN):
        print(f"找不到二进制 {BIN}，请先执行 make")
        return 2
    test_hand_triangle()
    test_contained()
    test_no_intersection()
    test_edge_coincidence()
    test_segment()
    test_point()
    test_self_intersect()
    test_bad_clip()
    test_bad_json()
    test_random()
    print(f"\n==== {CHECKS} 项断言，{len(FAILS)} 项失败 ====")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
