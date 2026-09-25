#!/usr/bin/env python3
"""test_cli.py — 对 sphdist 二进制做端到端测试。

覆盖：
  - distance：跨反经线 / 近极点 / 重合 / 对跖点 / 单位换算，
    期望值用 Python 独立实现的球面余弦定理（math，C 双精度）计算；
  - range：枚举数据集 brute-force 对照（bbox 仅粗筛，最终必须与
    精确距离一致），排序、candidate_count<=total、points_file；
  - 错误路径：坏 JSON、越界经纬度、缺字段、负半径、未知 action；
  - 退出码契约。
"""
import json
import math
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "sphdist")
DATA = os.path.join(ROOT, "data", "points_world.json")
R = 6371008.8  # 与 C++ 固定地球半径一致（米）
FAILURES = []
CHECKS = 0


def check(cond, name, detail=""):
    global CHECKS
    CHECKS += 1
    if not cond:
        FAILURES.append((name, detail))
        print(f"  [FAIL] {name} {detail}")


def call(req, expect_rc=0):
    p = subprocess.run(
        [BIN], input=json.dumps(req), capture_output=True, text=True, timeout=30
    )
    check(p.returncode == expect_rc,
          f"exit code {expect_rc}",
          f"got {p.returncode}, stderr={p.stderr.strip()}, stdout={p.stdout.strip()}")
    try:
        return json.loads(p.stdout)
    except json.JSONDecodeError:
        check(False, "response is JSON", p.stdout)
        return {}


def ref_distance(lat1, lon1, lat2, lon2):
    """独立参考：球面余弦定理（与 C++ haversine 不同公式）。"""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians((lon2 - lon1 + 180) % 360 - 180)
    cos_c = math.sin(p1) * math.sin(p2) + math.cos(p1) * math.cos(p2) * math.cos(dl)
    return R * math.acos(max(-1.0, min(1.0, cos_c)))


def expect_distance(name, a, b, expected=None, tol=1e-6):
    resp = call({"action": "distance", "a": {"lat": a[0], "lon": a[1]},
                 "b": {"lat": b[0], "lon": b[1]}})
    got = resp.get("distance_m")
    check(resp.get("ok") is True, name + " ok", str(resp))
    if expected is None:
        expected = ref_distance(a[0], a[1], b[0], b[1])
    check(got is not None and abs(got - expected) <= tol,
          name, f"expected {expected:.9f}, got {got}")
    # 单位换算检查：m / 1000 == km
    check(abs(resp["distance_km"] - resp["distance_m"] / 1000.0) < 1e-12,
          name + " km unit", "")
    check(resp["unit"] == "meter", name + " unit label", "")
    check(resp["earth_radius_m"] == R, name + " earth radius", "")


def main():
    print("CLI integration tests (sphdist)")

    if not os.path.exists(BIN):
        print(f"binary not found: {BIN}; run `make` first", file=sys.stderr)
        return 2

    half = math.pi * R                 # 半周长（对跖）
    quarter = math.pi / 2 * R         # 四分大圆
    one_deg = math.pi / 180 * R

    # --- 手算常量 / 单位 ---
    expect_distance("quarter great circle (equator->pole)",
                    (0, 0), (90, 0), quarter)
    expect_distance("one degree on equator", (0, 0), (0, 1), one_deg)

    # --- 重合 ---
    expect_distance("coincident point", (31.2304, 121.4737),
                    (31.2304, 121.4737), 0.0)
    expect_distance("coincident on +180/-180 meridian", (0, 180), (0, -180), 0.0)
    expect_distance("coincident north pole different lon", (90, 10), (90, 170), 0.0)
    expect_distance("coincident south pole different lon", (-90, 10), (-90, 170), 0.0)

    # --- 对跖 ---
    expect_distance("antipodal on equator", (0, 0), (0, 180), half)
    expect_distance("antipodal poles", (90, 0), (-90, 0), half)
    expect_distance("antipodal shanghai-ish",
                    (31.23, 121.47), (-31.23, -58.53), half, tol=1e-3)

    # --- 跨反经线 ---
    expect_distance("cross AM 2 degrees", (0, 179), (0, -179), 2 * one_deg, tol=1e-4)
    expect_distance("cross AM fiji",
                    (-17.7, 179.5), (-17.7, -179.5),
                    one_deg * math.cos(math.radians(-17.7)), tol=2.0)
    resp = call({"action": "distance",
                 "a": {"lat": 0, "lon": 179}, "b": {"lat": 0, "lon": -179}})
    check(resp["distance_m"] < 250_000,
          "cross AM shortest arc (not 358deg)", f"got {resp['distance_m']}")

    # --- 近极点：高纬经度差收缩 ---
    dlat = math.degrees(10_000.0 / R)
    lat = 90 - dlat
    expect_distance("near pole to pole ~10km", (lat, 0), (90, 0), 10_000.0, tol=1e-6)
    resp = call({"action": "distance",
                 "a": {"lat": lat, "lon": 0}, "b": {"lat": lat, "lon": 180}})
    check(19_990 < resp["distance_m"] < 20_010,
          "near pole 180 lon diff ~20km", f"got {resp['distance_m']}")

    # --- range：内联数据集 ---
    points = [
        {"id": "tokyo",   "lat": 35.68,  "lon": 139.69},
        {"id": "shanghai","lat": 31.23,  "lon": 121.47},
        {"id": "am-east", "lat": 0,      "lon": 179.9},
        {"id": "am-west", "lat": 0,      "lon": -179.9},
        {"id": "np-1",    "lat": 89.99,  "lon": 10},
        {"id": "np-2",    "lat": 89.99,  "lon": -170},
        {"id": "lima",    "lat": -12.05, "lon": -77.04},
    ]
    # 反经线场景：圆心 (0,180)，50km —— am-east/am-west 都应命中
    resp = call({"action": "range", "center": {"lat": 0, "lon": 180},
                 "radius_m": 50_000, "points": points})
    ids = [m["id"] for m in resp["matches"]]
    check(set(ids) == {"am-east", "am-west"},
          "range cross-AM both sides", str(ids))
    check(resp["candidate_bbox"]["crosses_antimeridian"] is True,
          "range bbox flagged crossing AM", "")

    # 北极场景：20km，np-1/np-2 都在（经度任意），无普通点
    resp = call({"action": "range", "center": {"lat": 90, "lon": 0},
                 "radius_m": 20_000, "points": points})
    ids = [m["id"] for m in resp["matches"]]
    check(set(ids) == {"np-1", "np-2"}, "range polar any longitude", str(ids))

    # --- range 通用 brute-force 对照（含边界值）---
    for center, radius in [((0, 0), 1_000_000), ((37.0, 140.0), 800_000),
                           ((-89.9, 45.0), 100_000), ((10, -179.5), 300_000)]:
        resp = call({"action": "range",
                     "center": {"lat": center[0], "lon": center[1]},
                     "radius_m": radius, "points": points})
        brute = [(p["id"], ref_distance(center[0], center[1], p["lat"], p["lon"]))
                 for p in points]
        expect_ids = sorted(i for i, d in brute if d <= radius + 1e-7)
        got_ids = sorted(m["id"] for m in resp["matches"])
        check(expect_ids == got_ids,
              f"range brute-force match center={center} r={radius}",
              f"expected {expect_ids} got {got_ids}")
        # 粗筛计数必须 >= 真实命中数且 <= 总数（证明只是候选过滤）
        check(resp["candidate_count"] >= len(expect_ids),
              "candidate_count >= true matches", "")
        check(resp["candidate_count"] <= resp["total_points"],
              "candidate_count <= total", "")
        # 每个报告距离必须与独立公式一致；结果按距离升序
        dists = [m["distance_m"] for m in resp["matches"]]
        check(dists == sorted(dists), "matches sorted by distance", str(dists))
        got_dist = {m["id"]: m["distance_m"] for m in resp["matches"]}
        for i, d in brute:
            if i in got_dist:
                check(abs(got_dist[i] - d) <= 1e-6 * max(1.0, d),
                      f"range distance {i}", f"{got_dist[i]} vs {d}")

    # --- radius=0：只包含重合点（退化）---
    resp = call({"action": "range",
                 "center": {"lat": 35.68, "lon": 139.69},
                 "radius_m": 0, "points": points})
    check([m["id"] for m in resp["matches"]] == ["tokyo"],
          "radius=0 returns only coincident", str(resp["matches"]))

    # --- points_file 入口 ---
    resp = call({"action": "range", "center": {"lat": 0, "lon": 0},
                 "radius_m": 20_000_000, "points_file": DATA})
    check(resp.get("ok") is True, "range with points_file", str(resp)[:200])
    # 20000km 足以覆盖全球数据集（半周长 ~20015km，边界附近用宽松检查）
    with open(DATA) as f:
        n_all = len(json.load(f)["points"])
    check(resp["total_points"] == n_all, "points_file count", "")

    # --- 错误路径（退出码 1）---
    bad_cases = [
        ("garbage json", b"{not json", 400),
    ]
    for name, raw, _ in bad_cases:
        p = subprocess.run([BIN], input=raw, capture_output=True, timeout=10)
        check(p.returncode == 1, name + " exit 1", f"rc={p.returncode}")
        check(json.loads(p.stdout)["ok"] is False, name + " ok=false", "")

    err_requests = [
        ("unknown action", {"action": "fly"}),
        ("missing action", {"a": {"lat": 0, "lon": 0}}),
        ("distance missing b", {"action": "distance", "a": {"lat": 0, "lon": 0}}),
        ("lat out of range", {"action": "distance",
                              "a": {"lat": 91, "lon": 0}, "b": {"lat": 0, "lon": 0}}),
        ("lon out of range", {"action": "distance",
                              "a": {"lat": 0, "lon": 200}, "b": {"lat": 0, "lon": 0}}),
        ("negative radius", {"action": "range", "center": {"lat": 0, "lon": 0},
                             "radius_m": -1, "points": []}),
        ("range no points", {"action": "range", "center": {"lat": 0, "lon": 0},
                             "radius_m": 100}),
        ("points_file missing", {"action": "range", "center": {"lat": 0, "lon": 0},
                                 "radius_m": 100, "points_file": "/nonexistent/x.json"}),
        ("non-number lat", {"action": "distance",
                            "a": {"lat": "x", "lon": 0}, "b": {"lat": 0, "lon": 0}}),
    ]
    for name, req in err_requests:
        p = subprocess.run([BIN], input=json.dumps(req), capture_output=True,
                           text=True, timeout=10)
        check(p.returncode == 1, name + " exit 1", f"rc={p.returncode}")
        body = json.loads(p.stdout)
        check(body.get("ok") is False and body.get("error", {}).get("code"),
              name + " structured error", p.stdout)

    # --- 文件参数入口与不存在文件（退出码 3）---
    p = subprocess.run([BIN, "/nonexistent/req.json"], capture_output=True, timeout=10)
    check(p.returncode == 3, "nonexistent request file exit 3", f"rc={p.returncode}")

    print(f"\n{CHECKS} checks, {len(FAILURES)} failures")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
