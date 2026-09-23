#!/usr/bin/env bash
# scenario.sh — 端到端演示：增量降采样、迟到修订传播、删除、空桶、
# 原始保留窗口驱逐（有损）与重启持久化恢复。
#
# 脚本自包含：自行 go build、启动临时服务、播种合成数据、逐项演示后关闭。
# 用法：bash examples/scenario.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PORT="${PORT:-18347}"
BASE="http://127.0.0.1:${PORT}"
WORK="$(mktemp -d)"
trap 'kill "${SRV_PID:-0}" 2>/dev/null || true' EXIT

cd "$ROOT"
go build -o "$WORK/server" ./cmd/server
go build -o "$WORK/seed"   ./cmd/seed

"$WORK/server" -addr=":${PORT}" -data="$WORK/data" -snapshot-interval=0 \
  >"$WORK/server.log" 2>&1 &
SRV_PID=$!
sleep 1

S="2026-09-23T17:00:00Z"; E="2026-09-23T19:59:59Z"
q() { curl -s "$BASE/v1/query?metric=cpu.usage&label.host=$1&start=$S&end=$E$2"; }

echo "### 1) 播种四种密度（dense/sparse/ragged/gappy），各 3 小时"
for d in dense sparse ragged gappy; do
  "$WORK/seed" -addr="$BASE" -density="$d" -hours=3 -seed=42 -host="$d" | head -1
done

echo; echo "### 2) 增量降采样 vs 原始重算（全密度 × 两层）"
bash examples/reconcile.sh "$BASE"

echo; echo "### 3) 迟到修订（同一样本 ID 改值为 500），向分钟/小时两层传播"
SID="ragged-ragged-s42-1790182800"
show() { python3 -c "
import json,sys
p=json.load(sys.stdin)['results'][0]['points'][0]
print('  count=%d sum=%.6f min=%.f max=%.f'%(p['count'],p['sum'],p['min'],p['max']))"; }
printf '修订前 17:00 分钟桶:'; q ragged "&step=60&fill=zero" | show
printf '修订前 17点  小时桶:'; q ragged "&step=3600&fill=zero" | show
curl -s -X POST "$BASE/v1/ingest" -d \
  "{\"samples\":[{\"id\":\"$SID\",\"metric\":\"cpu.usage\",\"labels\":{\"host\":\"ragged\"},\"ts\":1790182800,\"value\":500}]}" >/dev/null
printf '修订后 17:00 分钟桶:'; q ragged "&step=60&fill=zero" | show
printf '修订后 17点  小时桶:'; q ragged "&step=3600&fill=zero" | show
printf '原始重算 17点小时桶:'; q ragged "&step=3600&fill=zero&source=recompute" | show

echo; echo "### 4) 删除样本（显式订正），同样向两层传播"
curl -s -X POST "$BASE/v1/delete" -d "{\"ids\":[\"$SID\"]}" >/dev/null
printf '删除后 17:00 分钟桶:'; q ragged "&step=60&fill=zero" | show
printf '删除后 17点  小时桶:'; q ragged "&step=3600&fill=zero" | show
echo -n "已删除 ID 再写入 -> "; curl -s -X POST "$BASE/v1/ingest" -d \
  "{\"samples\":[{\"id\":\"$SID\",\"metric\":\"cpu.usage\",\"labels\":{\"host\":\"ragged\"},\"ts\":1790182800,\"value\":1}]}" \
  | python3 -c "import json,sys; print('rejected =', len(json.load(sys.stdin)['rejected']))"

echo; echo "### 5) 空桶可观测性（gappy 中间一小时无数据）"
q gappy "&step=3600&fill=zero" | python3 -c "
import json,sys
for p in json.load(sys.stdin)['results'][0]['points']:
    print('  ts=%d count=%d avg=%s'%(p['ts'],p['count'],p['avg']))"

echo; echo "### 6) 原始保留窗口驱逐（有损）：驱逐 18:00 前原始样本"
curl -s -X POST "$BASE/v1/evict" -d '{"before":"2026-09-23T18:00:00Z"}'
echo
printf '存储层 17点小时桶:'; q dense "&step=3600&fill=zero" | show
printf '原始重算 17点小时:'; q dense "&step=3600&fill=zero&source=recompute" | show
echo -n "被驱逐 ID 的迟到修订 -> "; curl -s -X POST "$BASE/v1/ingest" -d \
  '{"samples":[{"id":"dense-dense-s42-1790182800","metric":"cpu.usage","labels":{"host":"dense"},"ts":1790182800,"value":1}]}' \
  | python3 -c "import json,sys; print('rejected =', len(json.load(sys.stdin)['rejected']))"

echo; echo "### 7) 重启持久化：杀进程后用同一数据目录重开"
kill "$SRV_PID"; wait "$SRV_PID" 2>/dev/null || true
"$WORK/server" -addr=":${PORT}" -data="$WORK/data" -snapshot-interval=0 \
  >"$WORK/server.log" 2>&1 &
SRV_PID=$!
sleep 1
curl -s "$BASE/healthz"; echo
printf '重启后 dense 18点小时桶:'
curl -s "$BASE/v1/query?metric=cpu.usage&label.host=dense&start=2026-09-23T18:00:00Z&end=2026-09-23T18:59:59Z&step=3600&fill=zero" | show
echo "（17 点聚合在驱逐后仍保留，原始样本不可恢复）"
