#!/usr/bin/env bash
# 一键演示：生成夹具 → 构建 → 启动服务 → 依次提交四个验收场景。
set -euo pipefail

ADDR="${PROVENANCE_LISTEN_ADDR:-127.0.0.1:18080}"
FIXTURES_DIR="${PROVENANCE_FIXTURES_DIR:-fixtures}"

cd "$(dirname "$0")"

echo "==> [1/4] 生成本地夹具（确定性身份、策略、示例请求）"
cargo run --quiet --bin gen-fixtures -- "$FIXTURES_DIR"

echo "==> [2/4] 构建服务"
cargo build --quiet --bin provenance-server

echo "==> [3/4] 启动服务（$ADDR）"
PROVENANCE_LISTEN_ADDR="$ADDR" PROVENANCE_FIXTURES_DIR="$FIXTURES_DIR" \
  ./target/debug/provenance-server &
SERVER_PID=$!
trap 'kill "$SERVER_PID" 2>/dev/null || true' EXIT

# 等待端口就绪（最多约 5 秒）。
for _ in $(seq 1 50); do
  curl -sf "http://$ADDR/healthz" >/dev/null 2>&1 && break
  sleep 0.1
done

echo "==> [4/4] 提交四个验收场景"
for f in 01_valid 02_output_substituted 03_material_missing 04_cross_builder_reuse; do
  echo
  echo "──────────────────────────────────────────────"
  echo "场景 $f（请求体: $FIXTURES_DIR/examples/$f.json）"
  curl -sS -X POST "http://$ADDR/v1/verify" \
    -H 'content-type: application/json' \
    --data-binary "@$FIXTURES_DIR/examples/$f.json" | python3 -c "
import json, sys
r = json.load(sys.stdin)
print('总判定:', r['verdict'])
for c in r['checks']:
    mark = 'PASS' if c['status'] == 'PASS' else 'FAIL'
    print(f'  [{mark}] {c[\"id\"]}: {c[\"detail\"]}')"
done

echo
echo "演示完成，停止服务。"
