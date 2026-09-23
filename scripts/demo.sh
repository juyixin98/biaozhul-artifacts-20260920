#!/usr/bin/env bash
# 端到端验收演示：启动服务 → 跑三个示例场景 → 打印结论。
# 用法：bash scripts/demo.sh
set -euo pipefail
cd "$(dirname "$0")/.."

PY=.venv/bin/python
DB=$(mktemp -u /tmp/finality-demo-XXXX.db)
PORT=8077
BASE="http://127.0.0.1:$PORT"
export FINALITY_DB="$DB"

cleanup() { [[ -n "${SERVER_PID:-}" ]] && kill "$SERVER_PID" 2>/dev/null || true; rm -f "$DB" "$DB-wal" "$DB-shm"; }
trap cleanup EXIT

echo "== 启动服务（FINALITY_DB=$DB）=="
.venv/bin/uvicorn app.main:app --port "$PORT" --log-level warning &
SERVER_PID=$!
for i in $(seq 1 50); do
  curl -sf "$BASE/health" >/dev/null 2>&1 && break
  sleep 0.2
done
curl -sf "$BASE/health" | $PY -m json.tool

post_votes() { # $1 = votes json 文件
  $PY - "$1" "$BASE" <<'EOF'
import json, sys, urllib.request
votes, base = json.load(open(sys.argv[1])), sys.argv[2]
for v in votes:
    req = urllib.request.Request(base + "/votes", data=json.dumps(v).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            body = json.load(r)
    except urllib.error.HTTPError as e:
        body = json.load(e)
    print(f"  vote {v['validator_id']} -> {v['block_hash'][:8]}... : {body}")
EOF
}

echo; echo "== 场景 1：正常终局（3x 权重 1，需 3 票）=="
curl -sf -X POST "$BASE/epochs" -H 'Content-Type: application/json' -d @examples/epoch1.json | $PY -m json.tool
post_votes examples/votes_epoch1.json
curl -sf "$BASE/epochs/1/status" | $PY -c "import json,sys; s=json.load(sys.stdin); print('状态:', s['state'], '| 终局块:', (s['finalized_block'] or '')[:16]+'...', '| 权重:', s['finalized_weight'])"

echo; echo "== 场景 2：终局冲突 → 告警并冻结 =="
curl -sf -X POST "$BASE/epochs" -H 'Content-Type: application/json' -d @examples/epoch2.json | $PY -m json.tool
post_votes examples/votes_epoch2_conflict.json
curl -sf "$BASE/epochs/2/status" | $PY -c "
import json,sys
s=json.load(sys.stdin)
print('状态:', s['state'])
print('检查点(不被覆盖):', (s['finalized_block'] or '')[:16]+'...')
c=s['conflict']
print('冲突块:', c['conflicting_block'][:16]+'...', '| 双投者:', c['equivocators'])"

echo; echo "== 场景 3：加权 + 零权重（10/10/0，需 >=14）=="
curl -sf -X POST "$BASE/epochs" -H 'Content-Type: application/json' -d @examples/epoch3.json | $PY -m json.tool
post_votes examples/votes_epoch3.json
curl -sf "$BASE/epochs/3/status" | $PY -c "import json,sys; s=json.load(sys.stdin); print('状态:', s['state'], '| 终局权重:', s['finalized_weight'], '/', s['total_weight'])"

echo; echo "== 检查点列表 =="
curl -sf "$BASE/checkpoints" | $PY -m json.tool

echo; echo "== 重启服务，验证重放重建同一结论 =="
kill "$SERVER_PID"; wait "$SERVER_PID" 2>/dev/null || true
.venv/bin/uvicorn app.main:app --port "$PORT" --log-level warning &
SERVER_PID=$!
for i in $(seq 1 50); do
  curl -sf "$BASE/health" >/dev/null 2>&1 && break
  sleep 0.2
done
curl -sf "$BASE/health" | $PY -m json.tool
curl -sf "$BASE/epochs/2/status" | $PY -c "import json,sys; s=json.load(sys.stdin); print('重启后 epoch2 状态:', s['state'], '| 检查点仍为:', (s['finalized_block'] or '')[:16]+'...')"
echo; echo "演示完成。"
