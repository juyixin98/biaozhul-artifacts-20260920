#!/usr/bin/env python3
"""端到端测试: 通过真实 CLI 二进制验证 JSON 请求/响应契约与几何正确性。

覆盖: 矩形手算案例、全包含、无交、沿边重合(有面积/零面积)、点接触、
CW 输入方向统一、自交拒绝、非凸裁剪拒绝、非法 JSON/请求、退出码。
"""
import json
import math
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BIN = os.path.join(ROOT, "build", "polygon-clip")

failures = []
checks = 0


def check(cond, msg):
    global checks
    checks += 1
    if not cond:
        failures.append(msg)
        print(f"FAIL: {msg}")


def run_request(req):
    """发送 JSON 请求, 返回 (exit_code, parsed_response)."""
    p = subprocess.run(
        [BIN],
        input=json.dumps(req),
        capture_output=True,
        text=True,
    )
    try:
        resp = json.loads(p.stdout)
    except json.JSONDecodeError:
        resp = None
    return p.returncode, resp


def run_raw(text):
    p = subprocess.run([BIN], input=text, capture_output=True, text=True)
    try:
        resp = json.loads(p.stdout)
    except json.JSONDecodeError:
        resp = None
    return p.returncode, resp


def clip_request(subject, clip, **kw):
    req = {"subject": subject, "clip": clip}
    req.update(kw)
    return req


SQUARE = [[0, 0], [10, 0], [10, 10], [0, 10]]


def approx(a, b, tol=1e-9):
    return abs(a - b) <= tol


def pt_in(p, x, y, tol=1e-9):
    return approx(p[0], x, tol) and approx(p[1], y, tol)


def has_pt(verts, x, y, tol=1e-9):
    return any(pt_in(v, x, y, tol) for v in verts)


def main():
    if not os.path.exists(BIN):
        print(f"missing binary: {BIN} (run `make` first)")
        return 1

    # ---- 1. 矩形手算案例: 三角形被矩形裁剪 -> 五边形 ----
    code, r = run_request(clip_request([[2, -2], [18, 4], [6, 16]], SQUARE))
    check(code == 0, f"hand case exit code {code}")
    check(r["status"] == "ok", "hand case status ok")
    res = r["result"]
    check(res["kind"] == "polygon", "hand case kind polygon")
    check(res["orientation"] == "CCW", "hand case orientation CCW")
    check(res["vertex_count"] == 5, f"hand case 5 vertices, got {res['vertex_count']}")
    v = res["vertices"]
    for x, y in [(22 / 3, 0), (10, 1), (10, 10), (14 / 3, 10), (22 / 9, 0)]:
        check(has_pt(v, x, y), f"hand case vertex ({x},{y})")
    check(approx(r["metrics"]["area"], 568 / 9, 1e-8),
          f"hand case area 568/9, got {r['metrics']['area']}")

    # ---- 2. 全包含 ----
    code, r = run_request(clip_request([[1, 1], [3, 1], [3, 3], [1, 3]], SQUARE))
    check(code == 0 and r["result"]["kind"] == "polygon", "containment kind")
    check(approx(r["metrics"]["area"], 4.0), "containment area 4")
    check(r["result"]["vertex_count"] == 4, "containment 4 vertices")

    # ---- 3. 无交 ----
    code, r = run_request(
        clip_request([[20, 20], [30, 20], [30, 30], [20, 30]], SQUARE))
    check(code == 0, "disjoint exit 0")
    check(r["result"]["kind"] == "empty", "disjoint kind empty")
    check(r["result"]["vertices"] == [], "disjoint no vertices")
    check(r["metrics"]["area"] == 0, "disjoint area 0")

    # ---- 4. 沿边重合(有面积) ----
    code, r = run_request(clip_request([[2, 0], [8, 0], [8, 6], [2, 6]], SQUARE))
    check(code == 0 and r["result"]["kind"] == "polygon", "edge-overlap kind")
    check(approx(r["metrics"]["area"], 36.0), "edge-overlap area 36")
    check(r["result"]["vertex_count"] == 4, "edge-overlap 4 vertices")

    # ---- 5. 沿边重合(零面积): 仅一条边接触 -> segment ----
    code, r = run_request(clip_request([[2, 0], [8, 0], [5, -6]], SQUARE))
    check(code == 0, "segment-touch exit 0")
    check(r["result"]["kind"] == "segment", f"segment-touch kind, got {r['result']['kind']}")
    check(r["result"]["vertex_count"] == 2, "segment-touch 2 vertices")
    check(r["metrics"]["area"] == 0, "segment-touch area 0")
    check(has_pt(r["result"]["vertices"], 2, 0) and has_pt(r["result"]["vertices"], 8, 0),
          "segment-touch endpoints (2,0),(8,0)")

    # ---- 6. 点接触 -> point ----
    code, r = run_request(clip_request([[0, 10], [-10, 20], [-5, 12]], SQUARE))
    check(code == 0, "point-touch exit 0")
    check(r["result"]["kind"] == "point", f"point-touch kind, got {r['result']['kind']}")
    check(r["result"]["vertex_count"] == 1, "point-touch 1 vertex")
    check(has_pt(r["result"]["vertices"], 0, 10), "point-touch at (0,10)")

    # ---- 7. CW 输入: 输出仍 CCW ----
    code, r = run_request(clip_request([[1, 1], [1, 3], [3, 3], [3, 1]], SQUARE))
    check(code == 0 and r["result"]["orientation"] == "CCW", "cw input -> ccw output")
    check(r["result"]["input_subject_orientation"] == "CW", "cw input reported")
    check(approx(r["metrics"]["area"], 4.0), "cw input area 4")

    # ---- 8. 自交主体被拒绝 ----
    code, r = run_request(clip_request([[1, 1], [9, 9], [9, 1], [1, 9]], SQUARE))
    check(code == 4, f"self-intersect exit 4, got {code}")
    check(r["status"] == "error", "self-intersect status error")
    check(r["error"]["code"] == "INVALID_SUBJECT", "self-intersect code")

    # ---- 9. 非凸裁剪被拒绝 ----
    code, r = run_request(
        clip_request([[1, 1], [9, 1], [9, 9], [1, 9]],
                     [[0, 0], [10, 0], [5, 5], [10, 10], [0, 10]]))
    check(code == 4 and r["error"]["code"] == "INVALID_CLIP", "non-convex clip rejected")

    # ---- 10. 凹主体断连结果: 明确错误 ----
    u = [[0, 0], [10, 0], [10, 10], [6, 10], [6, 4], [4, 4], [4, 10], [0, 10]]
    code, r = run_request(clip_request(u, [[0, 6], [10, 6], [10, 9], [0, 9]]))
    check(code == 4, f"disconnected exit 4, got {code}")
    check(r["error"]["code"] in ("RESULT_MULTIPLE_COMPONENTS", "RESULT_NOT_SIMPLE"),
          f"disconnected code, got {r['error']['code']}")

    # ---- 11. 非法 JSON ----
    code, r = run_raw("{not json")
    check(code == 2 and r["error"]["code"] == "INVALID_JSON", "bad json exit 2")

    # ---- 12. 结构错误: 缺字段 ----
    code, r = run_raw(json.dumps({"subject": SQUARE}))
    check(code == 3 and r["error"]["code"] == "INVALID_REQUEST", "missing clip exit 3")

    # ---- 13. 结构错误: 非有限数 ----
    code, r = run_raw(json.dumps(
        {"subject": [[0, 0], [1e999, 0], [0, 1]], "clip": SQUARE}))
    check(code in (2, 3), f"non-finite exit {code}")

    # ---- 14. 旋转正方形(菱形)裁剪器 ----
    diamond = [[0, -10], [10, 0], [0, 10], [-10, 0]]
    code, r = run_request(clip_request([[-3, -3], [3, -3], [3, 3], [-3, 3]], diamond))
    check(code == 0 and approx(r["metrics"]["area"], 36.0), "diamond clip area 36")

    # ---- 15. decimal_places 输出舍入 ----
    code, r = run_request(clip_request([[2, -2], [18, 4], [6, 16]], SQUARE,
                                       decimal_places=3))
    check(code == 0, "decimal_places exit 0")
    for v in r["result"]["vertices"]:
        for c in v:
            check(approx(c, round(c, 3)), "decimal_places rounding applied")

    # ---- 16. 面积界: 0 <= area <= min(area(subject), area(clip)) ----
    for subj, clip in [
        ([[2, -2], [18, 4], [6, 16]], SQUARE),
        ([[5, 5], [15, 5], [15, 15], [5, 15]], SQUARE),
        ([[1, 1], [3, 1], [3, 3], [1, 3]], SQUARE),
    ]:
        code, r = run_request(clip_request(subj, clip))
        check(code == 0, "area-bound exit 0")
        a = r["metrics"]["area"]
        asub = abs(poly_area(subj))
        aclip = abs(poly_area(clip))
        check(-1e-9 <= a <= min(asub, aclip) + 1e-9,
              f"area bound 0 <= {a} <= {min(asub, aclip)}")
        # 每个结果点都在裁剪域内
        for v in r["result"]["vertices"]:
            check(-1e-9 <= v[0] <= 10 + 1e-9 and -1e-9 <= v[1] <= 10 + 1e-9,
                  f"vertex {v} inside clip domain")

    print(f"\ne2e tests: {checks} checks, {len(failures)} failure(s)")
    return 0 if not failures else 1


def poly_area(pts):
    s = 0.0
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return s / 2


if __name__ == "__main__":
    sys.exit(main())
