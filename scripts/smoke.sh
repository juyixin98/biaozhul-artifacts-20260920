#!/usr/bin/env bash
# 端到端冒烟测试：在临时端口启动服务，发送全部 samples，校验状态码与关键结果。
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"
bash scripts/build.sh >/dev/null

PORT="$(python3 - <<'PY'
import socket
s = socket.socket()
s.bind(('127.0.0.1', 0))
print(s.getsockname()[1])
s.close()
PY
)"

java -cp build/classes com.tjoin.service.Main "$PORT" > build/smoke-server.log 2>&1 &
PID=$!
# shellcheck disable=SC2064
trap "kill $PID 2>/dev/null || true" EXIT

for _ in $(seq 1 50); do
  curl -sf "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 && break
  sleep 0.1
done

fail=0
check() { # desc expected actual
  if [ "$2" == "$3" ]; then echo "PASS: $1 ($3)"; else echo "FAIL: $1 expected $2 got $3"; fail=1; fi
}

h=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:$PORT/health)
check "GET /health" 200 "$h"

for f in simple-join stalled-side exclusive-bounds duplicates; do
  code=$(curl -s -o "build/smoke-$f.json" -w '%{http_code}' \
      -X POST http://127.0.0.1:$PORT/join -H 'Content-Type: application/json' -d @"samples/$f.json")
  check "POST /join $f" 200 "$code"
done
code=$(curl -s -o build/smoke-buffer-capacity.json -w '%{http_code}' \
    -X POST http://127.0.0.1:$PORT/join -H 'Content-Type: application/json' \
    -d @samples/buffer-capacity.json)
check "POST /join buffer-capacity (cap exceeded)" 422 "$code"

# 语义抽查
pairs=$(python3 -c "import json;print(len(json.load(open('build/smoke-stalled-side.json'))['results']))")
check "stalled-side: L1/L2 retained and both matched R1 (2 pairs)" 2 "$pairs"
excl=$(python3 -c "import json;print(len(json.load(open('build/smoke-exclusive-bounds.json'))['results']))")
check "exclusive-bounds: only inner pair (1)" 1 "$excl"
dups=$(python3 -c "import json;print(json.load(open('build/smoke-duplicates.json'))['metrics']['duplicates'])")
check "duplicates: duplicate counter = 2" 2 "$dups"

exit $fail
