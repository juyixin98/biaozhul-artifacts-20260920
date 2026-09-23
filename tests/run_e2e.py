#!/usr/bin/env python3
"""run_e2e.py - simplify 可执行文件的端到端测试。

独立用 Python 重算几何量（不调用被测程序的距离函数），逐项核对：
  * 每个原始点到简化折线的距离 <= epsilon（+ 数值舍入容差）；
  * 简化点是原始点（去重后）的子集且顺序一致、端点保留；
  * 去重下标正确；
  * 重复运行结果逐字节一致（确定性）；
  * 错误输入返回 status=error 与非零退出码。

用法: python3 tests/run_e2e.py --bin ./build/simplify
"""

import argparse
import json
import math
import os
import subprocess
import sys
import tempfile

FAILURES = []
CHECKS = 0


def check(cond, name, detail=""):
    global CHECKS
    CHECKS += 1
    if not cond:
        FAILURES.append((name, detail))
        print(f"  [FAIL] {name} {detail}")


def point_to_segment(p, a, b):
    """与 C++ 端相同约定：点到闭线段距离（垂足在线段外则取端点）。"""
    abx, aby = b[0] - a[0], b[1] - a[1]
    len2 = abx * abx + aby * aby
    if not len2 > 0.0:  # 零长度段
        return math.hypot(p[0] - a[0], p[1] - a[1])
    t = ((p[0] - a[0]) * abx + (p[1] - a[1]) * aby) / len2
    t = min(1.0, max(0.0, t))
    cx, cy = a[0] + t * abx, a[1] + t * aby
    return math.hypot(p[0] - cx, p[1] - cy)


def point_to_polyline(p, poly):
    if len(poly) == 0:
        return float("inf")
    if len(poly) == 1:
        return math.hypot(p[0] - poly[0][0], p[1] - poly[0][1])
    return min(point_to_segment(p, poly[i], poly[i + 1])
               for i in range(len(poly) - 1))


def run(bin_path, payload):
    proc = subprocess.run(
        [bin_path], input=json.dumps(payload), capture_output=True,
        text=True, timeout=30)
    return proc.returncode, json.loads(proc.stdout), proc.stderr


def run_raw(bin_path, text):
    proc = subprocess.run(
        [bin_path], input=text, capture_output=True, text=True, timeout=30)
    return proc.returncode, proc.stdout, proc.stderr


def verify_success(name, payload, expected_count=None,
                   expected_source_indices=None):
    """通用成功用例：独立重算并核对响应的每一项误差结论。"""
    rc, out, err = run(BIN, payload)
    check(rc == 0, f"{name}: exit code 0", f"rc={rc} stderr={err!r}")
    check(out.get("status") == "ok", f"{name}: status ok",
          f"got={out.get('status')}")
    if out.get("status") != "ok":
        return out

    pts = [(p["x"], p["y"]) for p in payload["points"]]
    eps = float(payload["epsilon"])

    # 独立去重
    dedup = []
    removed = []
    for i, p in enumerate(pts):
        if dedup and dedup[-1] == p:
            removed.append(i)
        else:
            dedup.append(p)

    check(out["input_point_count"] == len(pts),
          f"{name}: input_point_count")
    check(out["deduplicated_point_count"] == len(dedup),
          f"{name}: deduplicated_point_count",
          f"got={out['deduplicated_point_count']} want={len(dedup)}")
    check(out["removed_duplicate_point_indices"] == removed,
          f"{name}: removed indices",
          f"got={out['removed_duplicate_point_indices']} want={removed}")

    kept = [(p["x"], p["y"]) for p in out["simplified"]]
    src = [p["source_index"] for p in out["simplified"]]

    # 子集且顺序一致（基于 source_index）
    check(all(0 <= s < len(dedup) for s in src),
          f"{name}: source indices in range")
    check(src == sorted(src) and len(src) == len(set(src)),
          f"{name}: source indices strictly increasing")
    check(all(dedup[s] == kept[i] for i, s in enumerate(src)),
          f"{name}: kept coords match source")

    # 端点保留
    if dedup:
        check(kept[0] == dedup[0] and kept[-1] == dedup[-1],
              f"{name}: endpoints retained")

    if expected_count is not None:
        check(len(kept) == expected_count,
              f"{name}: kept count", f"got={len(kept)} want={expected_count}")
    if expected_source_indices is not None:
        check(src == expected_source_indices,
              f"{name}: source indices exact",
              f"got={src} want={expected_source_indices}")

    # 逐点独立复核距离与 within_bound 标志
    scale = max([eps] + [abs(c) for p in pts for c in p] + [1.0])
    tol = 16.0 * sys.float_info.epsilon * scale
    v = out["verification"]
    check(v["bound_semantics"] == "one_sided_original_to_simplified",
          f"{name}: declares one-sided bound only")
    check(abs(v["bound"] - eps) <= 1e-15 * max(1.0, abs(eps)),
          f"{name}: bound echoed")

    reported = v["points"]
    check(len(reported) == len(pts),
          f"{name}: verification covers every input point")

    max_d = 0.0
    all_ok = True
    for i, p in enumerate(pts):
        rec = reported[i]
        check(rec["index"] == i, f"{name}: record index order at {i}")
        d = point_to_polyline(p, kept)
        check(math.isclose(rec["distance"], d, rel_tol=1e-12, abs_tol=1e-12),
              f"{name}: independent distance at point {i}",
              f"reported={rec['distance']} python={d}")
        within = d <= eps + tol
        check(rec["within_bound"] == within,
              f"{name}: within_bound flag at point {i}")
        max_d = max(max_d, d)
        all_ok = all_ok and within

    check(math.isclose(v["max_distance"], max_d, rel_tol=1e-12, abs_tol=1e-12),
          f"{name}: max_distance matches independent compute",
          f"got={v['max_distance']} python={max_d}")
    check(v["within_bound"] == all_ok,
          f"{name}: overall within_bound flag")
    check(all_ok, f"{name}: ONE-SIDED BOUND max_d <= eps+tol",
          f"max_d={max_d!r} eps={eps!r} tol={tol!r}")
    return out


def test_basic():
    # 基本锯齿
    verify_success("basic-zigzag", {
        "epsilon": 0.5,
        "points": [{"x": x, "y": (1 if x % 2 else 0)} for x in range(5)],
    }, expected_count=5)
    verify_success("basic-zigzag-loose", {
        "epsilon": 2.0,
        "points": [{"x": x, "y": (1 if x % 2 else 0)} for x in range(5)],
    }, expected_count=2, expected_source_indices=[0, 4])


def test_foldback():
    # 对称 U 形回折，非相邻重复点 (2,0)
    pts = [(0, 0), (2, 0), (2, -1), (2, 0), (4, 0)]
    payload = lambda e: {"epsilon": e,
                         "points": [{"x": x, "y": y} for x, y in pts]}
    verify_success("foldback-small", payload(0.25), expected_count=5)
    verify_success("foldback-mid", payload(0.95), expected_count=3,
                   expected_source_indices=[0, 2, 4])
    verify_success("foldback-large", payload(2.0), expected_count=2)

    # 更复杂的带回折折线（多次折返）
    pts2 = [(0, 0), (3, 0), (3, 1), (1, 1), (1, 0), (3, 0), (6, 0)]
    verify_success("foldback-multi", {
        "epsilon": 0.1,
        "points": [{"x": x, "y": y} for x, y in pts2],
    })


def test_self_intersection():
    # 蝴蝶结（自交）：(0,0)->(4,4)->(4,0)->(0,4)
    pts = [(0, 0), (4, 4), (4, 0), (0, 4)]
    verify_success("self-intersect-tight", {
        "epsilon": 1.0,
        "points": [{"x": x, "y": y} for x, y in pts],
    }, expected_count=4)
    verify_success("self-intersect-loose", {
        "epsilon": 5.0,
        "points": [{"x": x, "y": y} for x, y in pts],
    }, expected_count=2, expected_source_indices=[0, 3])

    # 自交五角星风格折线（小阈值应保留全部点且逐点满足界）
    star = [(0, 0), (4, 0), (1, 3), (2, -1), (3, 3), (0, 0)]
    verify_success("self-intersect-spike", {
        "epsilon": 0.01,
        "points": [{"x": x, "y": y} for x, y in star],
    })


def test_duplicates():
    # 相邻重复点
    pts = [(0, 0), (0, 0), (1, 1), (1, 1), (1, 1), (2, 0), (2, 0)]
    out = verify_success("duplicates-consecutive", {
        "epsilon": 0.5,
        "points": [{"x": x, "y": y} for x, y in pts],
    }, expected_count=3, expected_source_indices=[0, 1, 2])

    # 重复点的验证距离应为 0（被保留点对应）
    if out:
        distances = {rec["index"]: rec["distance"]
                     for rec in out["verification"]["points"]}
        check(math.isclose(distances[1], 0.0, abs_tol=1e-15),
              "duplicates: removed duplicate point distance 0")
        check(math.isclose(distances[6], 0.0, abs_tol=1e-15),
              "duplicates: trailing duplicate distance 0")

    # 全是同一点
    verify_success("all-identical", {
        "epsilon": 1.0,
        "points": [{"x": 5, "y": 5}, {"x": 5, "y": 5}, {"x": 5, "y": 5}],
    }, expected_count=1, expected_source_indices=[0])

    # 空点数组（退化）
    verify_success("empty-points", {"epsilon": 1.0, "points": []},
                   expected_count=0)

    # 单点
    verify_success("single-point", {"epsilon": 0.0,
                                    "points": [{"x": -1.5, "y": 2.25}]},
                   expected_count=1)


def test_epsilon_zero():
    # eps=0：共线去点、非共线全保留
    col = [(i, 0) for i in range(6)]
    verify_success("zero-eps-collinear", {
        "epsilon": 0,
        "points": [{"x": x, "y": y} for x, y in col],
    }, expected_count=2, expected_source_indices=[0, 5])

    zig = [(0, 0), (1, 1), (2, 0), (3, 1), (4, 0)]
    verify_success("zero-eps-zigzag", {
        "epsilon": 0.0,
        "points": [{"x": x, "y": y} for x, y in zig],
    }, expected_count=5, expected_source_indices=list(range(5)))

    # eps=0 + 重复点：重复点去掉，共线点去掉
    mixed = [(0, 0), (0, 0), (1, 0), (2, 0), (2, 0), (3, 0)]
    verify_success("zero-eps-dup-collinear", {
        "epsilon": 0,
        "points": [{"x": x, "y": y} for x, y in mixed],
    }, expected_count=2)


def test_errors():
    cases = [
        ("missing-epsilon", {"points": [{"x": 0, "y": 0}]}, 1),
        ("negative-epsilon", {"epsilon": -1, "points": []}, 1),
        ("epsilon-nan", {"epsilon": float("nan"),
                         "points": [{"x": 0, "y": 0}]}, 2),
        ("missing-points", {"epsilon": 1}, 1),
        ("points-not-array", {"epsilon": 1, "points": {}}, 1),
        ("point-missing-y", {"epsilon": 1, "points": [{"x": 0}]}, 1),
        ("point-inf", {"epsilon": 1,
                       "points": [{"x": 0, "y": float("inf")}]}, 2),
        ("epsilon-string", {"epsilon": "1", "points": []}, 1),
    ]
    for name, payload, want_rc in cases:
        raw = json.dumps(payload)
        rc, stdout, stderr = run_raw(BIN, raw)
        try:
            out = json.loads(stdout)
        except json.JSONDecodeError:
            check(False, f"{name}: JSON output", stdout)
            continue
        check(out.get("status") == "error", f"{name}: status error",
              f"got={out.get('status')}")
        check(rc == want_rc, f"{name}: exit code", f"got={rc} want={want_rc}")
        check(bool(out.get("error", {}).get("code")),
              f"{name}: error code present")

    # 非法 JSON 文本
    rc, stdout, _ = run_raw(BIN, "{not json")
    out = json.loads(stdout)
    check(out.get("status") == "error" and rc == 2,
          "invalid-json: error + exit 2", f"rc={rc}")

    # 空输入
    rc, stdout, _ = run_raw(BIN, "")
    out = json.loads(stdout)
    check(out.get("status") == "error" and rc == 2,
          "empty-input: error + exit 2")


def test_determinism():
    payload = {
        "epsilon": 0.3,
        "points": [{"x": x, "y": math.sin(x) * 2} for x in range(12)],
    }
    raw = json.dumps(payload)
    outputs = set()
    for _ in range(5):
        p = subprocess.run([BIN], input=raw, capture_output=True, text=True)
        outputs.add(p.stdout)
        check(p.returncode == 0, "determinism: rc 0")
    check(len(outputs) == 1, "determinism: 5 runs byte-identical")

    # 倒序输入应产生不同结果（确认不是固定输出），但各自满足界
    rev = {
        "epsilon": 0.3,
        "points": list(reversed(payload["points"])),
    }
    _, out1, _ = run(BIN, payload)
    _, out2, _ = run(BIN, rev)
    check(out1["simplified_count"] == out2["simplified_count"],
          "determinism: reversed input same count (symmetric data)")


def test_file_input():
    with tempfile.TemporaryDirectory() as tmp_dir:
        path = os.path.join(tmp_dir, "req.json")
        with open(path, "w") as f:
            json.dump({"epsilon": 0.1, "points": [{"x": 0, "y": 0},
                                                  {"x": 1, "y": 1},
                                                  {"x": 2, "y": 0}]}, f)
        p = subprocess.run([BIN, path], capture_output=True, text=True)
        out = json.loads(p.stdout)
        check(p.returncode == 0 and out["status"] == "ok",
              "file-input: works with filename argument")

        p2 = subprocess.run([BIN, os.path.join(tmp_dir, "nope.json")],
                            capture_output=True, text=True)
        out2 = json.loads(p2.stdout)
        check(p2.returncode == 2 and out2["status"] == "error",
              "file-input: missing file -> exit 2 error")


def main():
    global BIN
    ap = argparse.ArgumentParser()
    ap.add_argument("--bin", required=True)
    args = ap.parse_args()
    BIN = os.path.abspath(args.bin)

    print("Running end-to-end tests...")
    test_basic()
    test_foldback()
    test_self_intersection()
    test_duplicates()
    test_epsilon_zero()
    test_errors()
    test_determinism()
    test_file_input()

    print(f"{CHECKS} checks, {len(FAILURES)} failures")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
