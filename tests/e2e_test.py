#!/usr/bin/env python3
"""端到端测试：以子进程方式驱动 ray_box_backend，校验 JSON 输出。

覆盖验收要点：起点在盒内、擦边、负方向、退化盒、零方向分量、
最近命中 / 全部命中、错误输入、退出码，以及 BVH 与逐盒检测一致性标记。
仅依赖标准库；失败时以非零退出码结束。
"""
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "ray_box_backend")

failures = []
checks = 0


def call(payload, expect_code=0):
    global checks
    p = subprocess.run(
        [BIN], input=json.dumps(payload), capture_output=True, text=True
    )
    checks += 1
    if p.returncode != expect_code:
        failures.append(
            f"退出码 {p.returncode} != {expect_code}; stderr={p.stderr.strip()}"
        )
    try:
        return json.loads(p.stdout)
    except json.JSONDecodeError as e:
        failures.append(f"输出不是合法 JSON: {e}; raw={p.stdout!r}")
        return None


def ok(cond, msg):
    global checks
    checks += 1
    if not cond:
        failures.append(msg)


def close(a, b, rel=1e-9):
    return abs(a - b) <= rel * max(1.0, abs(a), abs(b))


def box(bid, lo, hi):
    return {"id": bid, "min": list(lo), "max": list(hi)}


def expect_error(payload, fragment):
    r = call(payload, expect_code=2)
    ok(r is not None and r.get("ok") is False, f"应返回错误: {fragment}")
    ok(fragment in (r or {}).get("error", ""),
       f"错误信息应包含 {fragment!r}，实际: {(r or {}).get('error')!r}")


# 1. 基本正向命中 + 最近命中
r = call({
    "mode": "nearest",
    "ray": {"origin": [-2, 0.5, 0.5], "direction": [1, 0, 0]},
    "boxes": [box(1, (0, 0, 0), (1, 1, 1)), box(2, (5, 5, 5), (6, 6, 6))],
})
ok(r and r["ok"] and r["hit"], "1: 应命中")
n = r["nearest"]
ok(n["id"] == 1, f"1: 最近 id=1，实际 {n['id']}")
ok(close(n["t_enter"], 2.0) and close(n["t_exit"], 3.0),
   f"1: t 应为 2/3，实际 {n['t_enter']}/{n['t_exit']}")
ok(all(close(a, b) for a, b in zip(n["point_enter"], [0, 0.5, 0.5])),
   f"1: 入射点错误 {n['point_enter']}")
ok(r["consistency_check"]["consistent"], "1: BVH 与逐盒应一致")

# 2. 起点在盒内：t_enter=0，inside=true，负方向出射
r = call({
    "mode": "nearest",
    "ray": {"origin": [1, 1, 1], "direction": [-1, 0, 0]},
    "boxes": [box(7, (0, 0, 0), (2, 2, 2))],
})
n = r["nearest"]
ok(n["inside"] is True and close(n["t_enter"], 0.0), "2: 应 inside 且 t_enter=0")
ok(close(n["t_exit"], 1.0), f"2: t_exit=1，实际 {n['t_exit']}")
ok(close(n["point_enter"][0], 1.0) and close(n["point_exit"][0], 0.0),
   "2: 出入射点错误")

# 3. 擦边（沿棱）与外角单点相切
r = call({
    "mode": "all",
    "ray": {"origin": [-2, 1, 1], "direction": [1, 0, 0]},
    "boxes": [box(1, (0, 0, 0), (1, 1, 1))],
})
ok(r["hit_count"] == 1, f"3: 沿棱擦边应命中 1 个，实际 {r['hit_count']}")
h = r["hits"][0]
ok(close(h["t_enter"], 2.0) and close(h["t_exit"], 3.0),
   "3: 沿棱擦边 t 应为 2/3（接触段）")

r = call({
    "mode": "all",
    "ray": {"origin": [2, 0.5, -2], "direction": [-1, 0, 1]},
    "boxes": [box(1, (0, 0, 0), (1, 1, 1))],
})
ok(r["hit_count"] == 1, "3b: 外角平分线单点相切应命中")
h = r["hits"][0]
ok(close(h["t_enter"], h["t_exit"]) and close(h["t_enter"], 2.8284271247461903),
   f"3b: 单点相切 t_enter=t_exit=2sqrt2，实际 {h['t_enter']}")

# 3c. 微小偏移即 miss
r = call({
    "mode": "nearest",
    "ray": {"origin": [-2, 1.001, 1], "direction": [1, 0, 0]},
    "boxes": [box(1, (0, 0, 0), (1, 1, 1))],
})
ok(r["hit"] is False, "3c: 外偏移应未命中")

# 4. 负方向命中（前面 2 已含；再测斜负方向）
r = call({
    "mode": "nearest",
    "ray": {"origin": [1.5, 1.5, 1.5], "direction": [-1, -1, -1]},
    "boxes": [box(1, (0, 0, 0), (1, 1, 1))],
})
n = r["nearest"]
ok(close(n["t_enter"], 0.8660254037844386), f"4: t_enter=sqrt3/2，实际 {n['t_enter']}")

# 5. 退化盒：薄片 / 线段 / 点
r = call({
    "mode": "all",
    "ray": {"origin": [1, 1, -1], "direction": [0, 0, 1]},
    "boxes": [
        box(10, (0, 0, 1), (2, 2, 1)),       # 薄片
        box(11, (0, 1, 1), (2, 1, 1)),       # 线段
        box(12, (1, 1, 1), (1, 1, 1)),       # 点
    ],
})
ids = sorted(h["id"] for h in r["hits"])
ok(ids == [10, 11, 12], f"5: 薄片/线段/点应全部命中，实际 {ids}")
for h in r["hits"]:
    ok(close(h["t_enter"], 2.0) and close(h["t_exit"], 2.0),
       f"5: 退化盒 {h['id']} 应单点相交 t=2，实际 {h['t_enter']}/{h['t_exit']}")

# 点盒偏离射线
r = call({
    "mode": "nearest",
    "ray": {"origin": [0, 0, 0], "direction": [1, 0, 0]},
    "boxes": [box(12, (1, 1, 1), (1, 1, 1))],
})
ok(r["hit"] is False, "5b: 偏离点盒应未命中")

# 6. 零方向分量（平行穿入 / 平行错过）
r = call({
    "mode": "nearest",
    "ray": {"origin": [0.5, -2, 0.5], "direction": [0, 1, 0]},
    "boxes": [box(1, (0, 0, 0), (1, 1, 1))],
})
ok(r["hit"] and close(r["nearest"]["t_enter"], 2.0), "6: 零分量穿入应命中 t=2")
r = call({
    "mode": "nearest",
    "ray": {"origin": [2, -2, 0.5], "direction": [0, 1, 0]},
    "boxes": [box(1, (0, 0, 0), (1, 1, 1))],
})
ok(r["hit"] is False, "6b: 零分量平行且在 slab 外应未命中")

# 6c. 盒在起点正后方：miss；起点恰在面上朝外：闭区间 t=0 点接触
r = call({
    "mode": "nearest",
    "ray": {"origin": [2, 0.5, 0.5], "direction": [1, 0, 0]},
    "boxes": [box(1, (0, 0, 0), (1, 1, 1))],
})
ok(r["hit"] is False, "6c: 盒在正后方应未命中")
r = call({
    "mode": "nearest",
    "ray": {"origin": [0, 0.5, 0.5], "direction": [-1, 0, 0]},
    "boxes": [box(1, (0, 0, 0), (1, 1, 1))],
})
n = r["nearest"]
ok(r["hit"] and n["inside"] and close(n["t_enter"], 0.0)
   and close(n["t_exit"], 0.0), "6d: 面上朝外应 t=0 点接触")

# 7. 全部命中排序（t 升序）与默认 id（下标）
r = call({
    "mode": "all",
    "ray": {"origin": [-2, 0.5, 0.5], "direction": [5, 0, 0]},
    "boxes": [
        {"min": [2, 0, 0], "max": [3, 1, 1]},   # 默认 id=0，较近
        {"min": [0, 0, 0], "max": [1, 1, 1]},   # 默认 id=1，较近
    ],
})
ids_order = [h["id"] for h in r["hits"]]
ts = [h["t_enter"] for h in r["hits"]]
ok(ids_order == [1, 0], f"7: 应按距离排序 id [1,0]，实际 {ids_order}")
ok(ts == sorted(ts), "7: t_enter 必须单调不减")
ok(r["consistency_check"]["consistent"], "7: 一致性检查应通过")

# 8. 错误输入：退出码 2 且 ok=false
expect_error(
    {"mode": "nearest",
     "ray": {"origin": [0, 0, 0], "direction": [0, 0, 0]},
     "boxes": []},
    "零向量",
)
expect_error(
    {"mode": "all",
     "ray": {"origin": [0, 0, 0], "direction": [1, 0, 0]},
     "boxes": [{"min": [2, 0, 0], "max": [1, 1, 1]}]},
    "min",
)
expect_error(
    {"mode": "nearest",
     "ray": {"origin": [0, 0, 0], "direction": [1, 0, 0]},
     "boxes": [{"id": 5, "min": [0, 0, 0], "max": [1, 1, 1]},
               {"id": 5, "min": [2, 2, 2], "max": [3, 3, 3]}]},
    "重复",
)
expect_error(
    {"mode": "weird",
     "ray": {"origin": [0, 0, 0], "direction": [1, 0, 0]},
     "boxes": []},
    "mode",
)
expect_error(
    {"mode": "nearest", "ray": {"origin": [0, 0, 0],
                                 "direction": [1, 0, 0]}},
    "boxes",
)

# 9. 非法 JSON 与空盒集
p = subprocess.run([BIN], input="{broken", capture_output=True, text=True)
ok(p.returncode == 2, f"9: 非法 JSON 退出码应为 2，实际 {p.returncode}")
ok(json.loads(p.stdout)["ok"] is False, "9: 非法 JSON 返回 ok=false")

r = call({"mode": "all",
          "ray": {"origin": [0, 0, 0], "direction": [1, 1, 1]},
          "boxes": []})
ok(r["ok"] and r["hit_count"] == 0, "9b: 空盒集合法且 0 命中")
ok(r["bvh_node_count"] == 0 and r["box_count"] == 0, "9b: 空 BVH 统计")

# 10. stats 字段存在且为非负整数
ok(isinstance(r["stats"]["boxes_tested"], int)
   and isinstance(r["stats"]["nodes_visited"], int), "10: stats 字段类型")

# 11. 大场景随机对照（Python 侧独立随机，检查 consistency 标记与剪枝）
import random
random.seed(42)
boxes_large = []
for i in range(2000):
    lo = [random.uniform(-50, 50) for _ in range(3)]
    hi = [lo[k] + random.uniform(0, 4) for k in range(3)]
    boxes_large.append({"id": i, "min": lo, "max": hi})
r = call({
    "mode": "all",
    "ray": {"origin": [-80, 0, 0], "direction": [1, 0.37, 0.21]},
    "boxes": boxes_large,
})
ok(r["consistency_check"]["consistent"], "11: 2000 盒场景一致性")
ok(r["box_count"] == 2000, "11: box_count")

print(f"端到端断言：{checks}，失败：{len(failures)}")
for f in failures:
    print("FAIL", f)
if failures:
    sys.exit(1)
print("全部通过。")
