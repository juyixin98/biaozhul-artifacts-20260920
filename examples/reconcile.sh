#!/usr/bin/env bash
# reconcile.sh — 对账：增量降采样层 vs 直接从原始样本重算。
#
# 对四种样本密度（dense/sparse/ragged/gappy）× 两层（分钟/小时），
# 逐桶比较 count/sum/min/max；count 必须精确相等，sum 允许 1e-9 相对
# 浮点容差（不同求和顺序的舍入差），min/max 精确相等。
#
# 用法：先启动服务并播种（见 00-*.http / seed 命令），再运行：
#   bash examples/reconcile.sh [base_url]
set -euo pipefail

B="${1:-http://127.0.0.1:8080}"
S="2026-09-23T17:00:00Z"
E="2026-09-23T19:59:59Z"

python3 - "$B" "$S" "$E" <<'PYEOF'
import json, sys, urllib.request

base, S, E = sys.argv[1], sys.argv[2], sys.argv[3]

def fetch(step, host, source=None):
    url = (f"{base}/v1/query?metric=cpu.usage&label.host={host}"
           f"&start={S}&end={E}&step={step}&fill=zero")
    if source:
        url += f"&source={source}"
    with urllib.request.urlopen(url) as r:
        return json.load(r)["results"][0]["points"]

def bucket_eq(a, b, eps=1e-9):
    if a["count"] != b["count"]:
        return False
    if a["count"] == 0:
        return True
    scale = max(1.0, abs(a["sum"]), abs(b["sum"]))
    return (abs(a["sum"] - b["sum"]) <= eps * scale
            and a["min"] == b["min"] and a["max"] == b["max"])

all_ok = True
for host in ["dense", "sparse", "ragged", "gappy"]:
    for step, name in (("60", "分钟"), ("3600", "小时")):
        stored = fetch(step, host)
        recomp = fetch(step, host, "recompute")
        same = (len(stored) == len(recomp)
                and all(bucket_eq(a, b) for a, b in zip(stored, recomp)))
        all_ok &= same
        empties = sum(1 for p in stored if p["count"] == 0)
        print(f"host={host:7s} {name}层 桶数={len(stored):3d} 空桶={empties:3d} "
              f"与原始重算一致(eps=1e-9): {same}")

print("\n总体:", "全部一致 ✅" if all_ok else "存在不一致 ❌")
sys.exit(0 if all_ok else 1)
PYEOF
