#!/usr/bin/env python3
"""端到端验收：通过真实 HTTP 接口校验 TF 坐标树服务。

用法:
    python3 scripts/e2e_test.py [base_url]

覆盖: 健康检查 / 静态边 / 动态乱序异步采样 / 区间插值解析核对 / 逆变换 /
链式组合 / 接近 180° / 环 / 多父 / 无效四元数 / 缺边 / 禁止外推 /
X-Content-SHA256 真实复算 / 状态码。
"""
import hashlib
import json
import math
import sys
import urllib.request
import urllib.error

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:18080"

fails = []
checks = 0


def check(cond, name):
    global checks
    checks += 1
    if not cond:
        fails.append(name)
        print(f"  [FAIL] {name}")


def call(method, path, payload=None):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            body = r.read().decode()
            return r.status, dict(r.headers), json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        return e.code, dict(e.headers), json.loads(body) if body else {}


def quat_rotate(q, v):
    w, x, y, z = q
    vx, vy, vz = v
    # q v q* （四元数向量部分公式）
    t = [
        (1 - 2 * (y * y + z * z)) * vx + 2 * (x * y - w * z) * vy +
        2 * (x * z + w * y) * vz,
        2 * (x * y + w * z) * vx + (1 - 2 * (x * x + z * z)) * vy +
        2 * (y * z - w * x) * vz,
        2 * (x * z - w * y) * vx + 2 * (y * z + w * x) * vy +
        (1 - 2 * (x * x + y * y)) * vz,
    ]
    return t


def vclose(a, b, eps=1e-8):
    return all(abs(x - y) <= eps for x, y in zip(a, b))


def qclose(q, r, eps=1e-8):
    d = abs(sum(a * b for a, b in zip(q, r)))
    return d >= 1 - eps


print("[e2e] 健康检查 + SHA-256 响应头")
st, hdr, j = call("GET", "/health")
check(st == 200 and j.get("status") == "ok", "GET /health 200")
sha_header = hdr.get("X-Content-SHA256")
raw = json.dumps(j, separators=(",", ":"))
# 服务端 body 与其 dump 一致（键序固定），直接对返回 body 原文复算：
# 重新取一次原始 body 以获得逐字节文本。
req = urllib.request.Request(BASE + "/health")
with urllib.request.urlopen(req, timeout=5) as r:
    raw_body = r.read()
    raw_sha = r.headers["X-Content-SHA256"]
check(hashlib.sha256(raw_body).hexdigest() == raw_sha,
      "X-Content-SHA256 与对响应体真实复算一致")

print("[e2e] 静态边 + 动态边（异步乱序发送）")
st, _, j = call("POST", "/edges/static",
                {"parent_frame": "world", "child_frame": "mount",
                 "translation": [0, 0, 1], "rotation": [1, 0, 0, 0]})
check(st == 200 and j.get("accepted") is True, "静态边 world->mount")

# 故意乱序 + 分两批异步 POST 发送
batch1 = [
    {"time": 4.0, "translation": [8, 0, 0], "rotation": [0, 0, 0, 1]},
    {"time": 0.0, "translation": [0, 0, 0], "rotation": [1, 0, 0, 0]},
]
batch2 = [
    {"time": 2.0, "translation": [4, 0, 0],
     "rotation": [math.cos(math.pi / 4), 0, 0, math.sin(math.pi / 4)]},
    {"time": 1.0, "translation": [2, 0, 0],
     "rotation": [math.cos(math.pi / 8), 0, 0, math.sin(math.pi / 8)]},
    {"time": 3.0, "translation": [6, 0, 0],
     "rotation": [math.cos(3 * math.pi / 8), 0, 0,
                  math.sin(3 * math.pi / 8)]},
]
st1, _, _ = call("POST", "/edges/dynamic",
                 {"parent_frame": "mount", "child_frame": "arm",
                  "samples": batch1})
st2, _, _ = call("POST", "/edges/dynamic",
                 {"parent_frame": "mount", "child_frame": "arm",
                  "samples": batch2})
check(st1 == 200 and st2 == 200, "动态样本分两批乱序提交全部接受")

print("[e2e] 区间插值 world->arm @ t=2.5 与解析解核对")
st, _, j = call("POST", "/query",
                {"source_frame": "world", "target_frame": "arm",
                 "time": 2.5})
check(st == 200, "插值查询 200")
# mount 原点 (0,0,1)；arm 局部 x 轴随时间绕 z 旋转，平移沿 mount-x：
# t=2.5 -> 角度 112.5°, arm 原点平移 (5,0,0)（mount 系），world 系 (5,0,1)
check(vclose(j["translation"], [5, 0, 1], 1e-9),
      f"插值平移=(5,0,1)，实际 {j['translation']}")
ang = 2.5 * math.pi / 4  # t=4 时 180°
q_expect = [math.cos(ang / 2), 0, 0, math.sin(ang / 2)]
rot = j["rotation"]
qv = [rot["w"], rot["x"], rot["y"], rot["z"]]
check(qclose(qv, q_expect), "插值旋转 = 绕 z 112.5°")
# 验证旋转作用于 x 轴
vx = quat_rotate(qv, [1, 0, 0])
check(vclose(vx, [math.cos(ang), math.sin(ang), 0], 1e-9),
      f"插值旋转把 x 轴转到 ({math.cos(ang):.6f},{math.sin(ang):.6f},0)")
check(abs(j["max_time_error"] - 0.5) < 1e-9,
      f"max_time_error=0.5，实际 {j['max_time_error']}")
modes = [e["mode"] for e in j["edges_used"]]
check(modes == ["static", "interpolated"], f"边模式 {modes}")
used = j["edges_used"][1]["sample_times_used"]
check(vclose(used, [2.0, 3.0], 1e-12), f"报告使用样本 [2,3]，实际 {used}")

print("[e2e] 逆变换 arm->world @ t=2.5")
st, _, jf = call("POST", "/query",
                 {"source_frame": "arm", "target_frame": "world",
                  "time": 2.5})
st, _, jb = call("POST", "/query",
                 {"source_frame": "world", "target_frame": "arm",
                  "time": 2.5})
# 组合正/逆矩阵应为单位阵（独立用 Python 重算 4x4 矩阵乘法）
def matmul(A, B):
    return [[sum(A[i][k] * B[k][j] for k in range(4)) for j in range(4)]
            for i in range(4)]
prod = matmul(jb["matrix"], jf["matrix"])
I = [[float(i == k) for k in range(4)] for i in range(4)]
check(all(abs(prod[i][k] - I[i][k]) < 1e-9 for i in range(4) for k in range(4)),
      "正向矩阵 × 逆向矩阵 = I（真实 4x4 复算）")

print("[e2e] 接近 180° 旋转（179.99°）中点插值")
call("POST", "/edges/static",
     {"parent_frame": "r180", "child_frame": "x1", "translation": [0, 0, 0],
      "rotation": [1, 0, 0, 0]})
a_deg = 179.99
call("POST", "/edges/dynamic", {"parent_frame": "r180", "child_frame": "s180",
    "samples": [
        {"time": 0, "translation": [0, 0, 0], "rotation": [1, 0, 0, 0]},
        {"time": 1, "translation": [0, 0, 0],
         "rotation": [math.cos(math.radians(a_deg) / 2), 0, 0,
                      math.sin(math.radians(a_deg) / 2)]},
    ]})
st, _, j = call("POST", "/query",
                {"source_frame": "r180", "target_frame": "s180",
                 "time": 0.5})
half = math.radians(a_deg / 2)
check(qclose([j["rotation"]["w"], j["rotation"]["x"], j["rotation"]["y"],
              j["rotation"]["z"]],
             [math.cos(half / 2), 0, 0, math.sin(half / 2)], 1e-7),
      "179.99° 中点 = 89.995°（无 NaN/无翻转跳变）")

print("[e2e] 拒绝：环 / 多父 / 无效四元数")
call("POST", "/edges/static",
     {"parent_frame": "a", "child_frame": "b", "translation": [0, 0, 0],
      "rotation": [1, 0, 0, 0]})
call("POST", "/edges/static",
     {"parent_frame": "b", "child_frame": "c", "translation": [0, 0, 0],
      "rotation": [1, 0, 0, 0]})
st, _, j = call("POST", "/edges/static",
                {"parent_frame": "c", "child_frame": "a",
                 "translation": [0, 0, 0], "rotation": [1, 0, 0, 0]})
check(st == 409 and j["error"] == "cycle_detected",
      f"成环返回 409 cycle_detected（实际 {st} {j.get('error')}）")

st, _, j = call("POST", "/edges/static",
                {"parent_frame": "other", "child_frame": "b",
                 "translation": [0, 0, 0], "rotation": [1, 0, 0, 0]})
check(st == 409 and j["error"] == "multiple_parent_conflict",
      f"多父冲突 409（实际 {st} {j.get('error')}）")

st, _, j = call("POST", "/edges/static",
                {"parent_frame": "p", "child_frame": "z",
                 "translation": [0, 0, 0], "rotation": [0, 0, 0, 0]})
check(st == 400 and j["error"] == "invalid_quaternion",
      f"零四元数 400 invalid_quaternion（实际 {st} {j.get('error')}）")

# NaN 不是合法 JSON 标量：请求体解析阶段即被拒绝（服务端默认不接受 NaN）。
nan_body = ('{"parent_frame":"p","child_frame":"nanf","samples":'
            '[{"time":0,"translation":[0,0,0],"rotation":[NaN,0,0,0]}]}')
req = urllib.request.Request(
    BASE + "/edges/dynamic", data=nan_body.encode(), method="POST",
    headers={"Content-Type": "application/json"})
try:
    urllib.request.urlopen(req, timeout=5)
    check(False, "NaN 四元数应被拒绝")
except urllib.error.HTTPError as e:
    j = json.loads(e.read().decode())
    check(e.code == 400 and j["error"] == "bad_request",
          f"NaN 四元数 400 bad_request（实际 {e.code}）")

print("[e2e] 缺边 / 不相连 / 禁止外推")
st, _, j = call("POST", "/query",
                {"source_frame": "arm", "target_frame": "ghost", "time": 0})
check(st == 422 and j["error"] == "unknown_frame",
      f"未知坐标系 422（实际 {st}）")

call("POST", "/edges/static",
     {"parent_frame": "iso1", "child_frame": "f1", "translation": [0, 0, 0],
      "rotation": [1, 0, 0, 0]})
call("POST", "/edges/static",
     {"parent_frame": "iso2", "child_frame": "f2", "translation": [0, 0, 0],
      "rotation": [1, 0, 0, 0]})
st, _, j = call("POST", "/query",
                {"source_frame": "f1", "target_frame": "f2", "time": 0})
check(st == 422 and j["error"] == "disconnected_tree",
      f"不相连树 422（实际 {st}）")

st, _, j = call("POST", "/query",
                {"source_frame": "world", "target_frame": "arm",
                 "time": -1.0})
check(st == 422 and j["error"] == "extrapolation_forbidden",
      f"向前外推 422（实际 {st}）")
st, _, j = call("POST", "/query",
                {"source_frame": "world", "target_frame": "arm", "time": 9.0})
check(st == 422 and j["error"] == "extrapolation_forbidden",
      f"向后外推 422（实际 {st}）")

# 容差内吸附并如实报告时间误差；容差外仍拒绝
st, _, j = call("POST", "/query",
                {"source_frame": "world", "target_frame": "arm",
                 "time": 4.02, "boundary_tolerance_seconds": 0.05})
check(st == 200 and j["edges_used"][1]["mode"] == "clamped_latest" and
      abs(j["max_time_error"] - 0.02) < 1e-9, "容差内吸附并报告 0.02s 误差")
st, _, j = call("POST", "/query",
                {"source_frame": "world", "target_frame": "arm",
                 "time": 4.2, "boundary_tolerance_seconds": 0.05})
check(st == 422, "超出容差仍拒绝外推")

print("[e2e] 协议其他项")
st, _, _ = call("GET", "/frames")
check(st == 200, "GET /frames 200")
st, _, _ = call("DELETE", "/health")
check(st == 405, "错误方法 405")
st, _, _ = call("GET", "/nope")
check(st == 404, "未知路径 404")
st, _, j = call("POST", "/edges/static", {"parent_frame": "x"})
check(st == 400 and j["error"] == "bad_request", "残缺 JSON 请求 400")

print(f"\n==== e2e: {checks} 项断言，{len(fails)} 项失败 ====")
sys.exit(1 if fails else 0)
