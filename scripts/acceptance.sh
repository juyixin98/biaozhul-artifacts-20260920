#!/usr/bin/env bash
# 验收脚本：自动化测试 + 构建 + 端到端 API 冒烟。
# 用法：./scripts/acceptance.sh
set -euo pipefail

cd "$(dirname "$0")/.."

# 选取一个当前空闲的高端口，避免与环境中其他服务冲突。
pick_port() {
  python3 - <<'PY'
import socket
s = socket.socket()
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
s.close()
PY
}
ADDR="127.0.0.1:$(pick_port)"
DATA_DIR="$(mktemp -d)"
BASE="http://$ADDR"

cleanup() {
  if [[ -n "${SERVER_PID:-}" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
  rm -rf "$DATA_DIR"
}
trap cleanup EXIT

echo "======== 1/5 cargo test（单元 + 集成）========"
cargo test --release

echo "======== 2/5 clippy（无警告）========"
cargo clippy --all-targets -- -D warnings

echo "======== 3/5 构建 release ========"
cargo build --release
BIN=./target/release/gas-meter

echo "======== 4/5 启动服务并冒烟 ========"
"$BIN" serve --addr "$ADDR" --data-dir "$DATA_DIR" >/tmp/gm-acceptance.log 2>&1 &
SERVER_PID=$!

# 等待端口就绪；进程提前退出则打印日志并失败。
for _ in $(seq 1 50); do
  if curl -sf "$BASE/health" >/dev/null 2>&1; then break; fi
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    cat /tmp/gm-acceptance.log
    echo "server exited before becoming ready" >&2
    exit 1
  fi
  sleep 0.1
done
curl -sf "$BASE/health" | grep -q '"ok"'
echo "  health OK ($ADDR)"

N_B64=$(python3 -c 'import base64;print(base64.b64encode((100000).to_bytes(8,"little")).decode())')

# 4.1 有限循环成功提交，返回 5000050000，diff 含 sum/hash。
R=$(curl -sf -X POST "$BASE/v1/transactions" -H 'content-type: application/json' \
  -d "{\"module_ref\":\"sample:finite_loop\",\"input_base64\":\"$N_B64\",\"metering_version\":1}")
echo "$R" | python3 -c '
import json,sys,base64
r=json.load(sys.stdin)
assert r["status"]=="committed", r
assert base64.b64decode(r["output"])==b"5000050000"
assert set(r["diff"]["upserts"])=={"sum","hash"}
f=r["fuel"]
assert f["consumed_total"]==f["wasm_instruction_fuel"]+f["host_call_fuel"]
assert "不是任何区块链的真实 Gas" in f["disclaimer"]
print("  finite_loop committed, output=5000050000, fuel bill conserved")
'

# 4.2 失败路径全部终止且不提交。
for sample in infinite_loop:out_of_fuel trap_after_write:trap memory_grow_v1:memory_limit_exceeded \
              oob_read:memory_out_of_bounds host_bad_ptr:memory_out_of_bounds denied_import:link_error; do
  name=${sample%%:*}; expected=${sample##*:}
  payload="{\"module_ref\":\"sample:$name\"}"
  [[ "$name" == "memory_grow_v1" ]] && payload="{\"module_ref\":\"sample:memory_grow\",\"input_base64\":\"AQAAAAAAAAA=\",\"metering_version\":1}"
  curl -sf -X POST "$BASE/v1/transactions" -H 'content-type: application/json' \
    -d "$payload" | python3 -c "
import json,sys
r=json.load(sys.stdin)
assert r['status']=='terminated', r
assert r['termination']=='$expected', (r['termination'],'$expected')
assert r['state_seq_after'] is None
assert not r['diff']['upserts']
print(f'  $name -> $expected (no commit)')
"
done

# 4.3 poison 缺席；state_seq 仍为 1。
curl -sf "$BASE/v1/state" | python3 -c '
import json,sys
r=json.load(sys.stdin)
assert r["state_seq"]==1, r
assert "poison" not in r["keys"]
assert set(r["keys"])=={"sum","hash"}
print("  rollback verified: poison absent, state_seq=1")
'

# 4.4 v2：结果一致，宿主费翻倍。
V2=$(curl -sf -X POST "$BASE/v1/transactions" -H 'content-type: application/json' \
  -d "{\"module_ref\":\"sample:finite_loop\",\"input_base64\":\"$N_B64\",\"metering_version\":2}")
echo "$V2" | python3 -c '
import json,sys
r=json.load(sys.stdin)
assert r["status"]=="committed"
assert r["output"]=="NTAwMDA1MDAwMA=="
print("  v2 output identical:",r["output"])
'

# 4.5 持久化文件存在且 JSON 合法。
python3 -c "
import json,glob
p=glob.glob('$DATA_DIR/state.json')[0]
d=json.load(open(p))
assert d['state_seq']==2 and set(d['kv'])=={'sum','hash'}
print('  state.json durable:',p)
"

echo "======== 5/5 二次运行确定性：相同输入+版本账单一致 ========"
R2=$(curl -sf -X POST "$BASE/v1/transactions" -H 'content-type: application/json' \
  -d "{\"module_ref\":\"sample:finite_loop\",\"input_base64\":\"$N_B64\",\"metering_version\":1,\"idempotency_key\":\"det\"}")
R3=$(curl -sf -X POST "$BASE/v1/transactions" -H 'content-type: application/json' \
  -d "{\"module_ref\":\"sample:finite_loop\",\"input_base64\":\"$N_B64\",\"metering_version\":1,\"idempotency_key\":\"det\"}")
python3 -c "
import json
a=json.loads('''$R2'''); b=json.loads('''$R3''')
assert a['output']==b['output']
assert a['fuel']['consumed_total']==b['fuel']['consumed_total']
assert a['determinism_key']==b['determinism_key']
print('  replay identical output + fuel bill:',a['fuel']['consumed_total'])
"

echo
echo "✅ 全部验收通过。"
