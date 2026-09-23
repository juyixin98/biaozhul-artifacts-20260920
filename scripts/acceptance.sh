#!/usr/bin/env bash
# 一键本地验收：签名输入 -> 提交流程 -> 迟到重算 -> 报告验签。
# 用法：
#   ./scripts/acceptance.sh                 # 内存存储（无需任何外部服务）
#   DATABASE_URL=postgresql+asyncpg://... ./scripts/acceptance.sh
set -euo pipefail
cd "$(dirname "$0")/.."

PORT="${PORT:-8021}"
BASE="http://127.0.0.1:${PORT}"
export ALLOW_RESET=1
export DATABASE_URL="${DATABASE_URL:-}"

# 本地直连，绕过代理
unset all_proxy ALL_PROXY http_proxy HTTP_PROXY https_proxy HTTPS_PROXY 2>/dev/null || true

[ -f keys/feeder_private.hex ] || .venv/bin/python scripts/generate_keys.py
.venv/bin/python scripts/sign_events.py examples/events_unsigned.jsonl examples/events.signed.jsonl >/dev/null
.venv/bin/python scripts/sign_events.py examples/late_event_unsigned.jsonl examples/late_event.signed.jsonl >/dev/null

.venv/bin/python -m uvicorn app.api:app --host 127.0.0.1 --port "${PORT}" >/tmp/lreplay_accept.log 2>&1 &
SRV=$!
trap 'kill ${SRV} 2>/dev/null || true' EXIT

for _ in $(seq 1 50); do
    curl -sf "${BASE}/health" >/dev/null && break
    sleep 0.2
done

echo "== health =="
curl -s "${BASE}/health"; echo

echo "== 提交主批次（24 个事件，含真实 Ed25519 签名）=="
.venv/bin/python - "$BASE" <<'EOF'
import json, sys, httpx
base = sys.argv[1]
evs = [json.loads(l) for l in open("examples/events.signed.jsonl") if l.strip()]
r = httpx.post(f"{base}/events", json={"events": evs}, timeout=10)
r.raise_for_status()
j = r.json()
assert j["accepted"] == 24 and j["replay_version"] == 1, j
print("v1:", j["accepted"], "events accepted, late =", j["late_detected"])

rep = httpx.get(f"{base}/reports/latest", timeout=10).json()["body"]["result"]
liqs = [(l["event_id"], l["position_id"]) for l in rep["liquidations"]]
skips = [(s["event_id"], s["reason"]) for s in rep["skipped"] if s["type"] == "liquidate"]
print("executed:", liqs)
print("skipped :", skips)
assert [e for e, _ in liqs] == ["e-liq-a-1", "e-liq-a-2"], "重复清算（每次 50%）"
assert ("e-liq-b-1", "healthy") in skips, "健康度恰好 1 不可清算"
assert ("e-liq-d-1", "healthy") in skips, "dan 健康"
assert ("e-liq-c-1", "missing_price") in skips, "无价格暂停"
assert ("e-liq-a-stale", "stale_price") in skips, "陈旧价格暂停"
bob = next(p for p in rep["positions"] if p["position_id"] == "bob")
# bob 在 t=200 同秒内：先借 100，再被清算；此刻债务恰 100、抵押恰 100，
# H 恰好 = 1 -> 按规则严格小于 1 才可清算，故以 healthy 拒绝。
# 报告快照计息到 t=230 后 H=0.99999994（利息累积所致）。
assert 999_999_000_000_000_000 < bob["health_ratio_wad"] < 10**18, bob["health_ratio_wad"]
EOF

echo
echo "== 提交迟到事件（ts=159 < 已处理最大 ts=230），触发版本化重算 =="
.venv/bin/python - "$BASE" <<'EOF'
import json, sys, httpx
base = sys.argv[1]
late = [json.loads(l) for l in open("examples/late_event.signed.jsonl") if l.strip()]
j = httpx.post(f"{base}/events", json={"events": late}, timeout=10).json()
assert j["late_detected"] is True and j["replay_version"] == 2, j
print("late recompute -> version", j["replay_version"])

v1 = httpx.get(f"{base}/reports/1", timeout=10).json()
v2 = httpx.get(f"{base}/reports/2", timeout=10).json()
assert len(v1["body"]["result"]["liquidations"]) == 2
assert len(v2["body"]["result"]["liquidations"]) == 0, "迟到抵押注入后 alice 变健康"
assert v2["prev_digest"] == v1["digest"], "报告哈希链"
print("v1 保留 2 笔清算，v2 为 0 笔，旧报告完整保留，哈希链相连")
EOF

echo
echo "== 报告 Ed25519 验签（含防篡改）=="
BASE_URL="${BASE}" .venv/bin/python scripts/verify_report.py

echo
echo "✅ 验收全部通过"
