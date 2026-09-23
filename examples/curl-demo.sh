#!/usr/bin/env bash
# 通过 HTTP JSON 接口完成同样的流程。
# 用法: bash examples/curl-demo.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_REPO="$HERE/.."
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
cp -r "$ROOT_REPO/testdata/demo" "$WORK/demo"

go -C "$ROOT_REPO" build -o "$WORK/depscanner" ./cmd/depscanner
PORT=18765
"$WORK/depscanner" serve --addr 127.0.0.1:$PORT &
SRV=$!
trap 'kill $SRV 2>/dev/null || true; rm -rf "$WORK"' EXIT

# 等待服务就绪（最多 5 秒）
for _ in $(seq 1 50); do
  if curl -sf "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then break; fi
  if ! kill -0 "$SRV" 2>/dev/null; then
    echo "server failed to start" >&2; exit 1
  fi
  sleep 0.1
done

base="http://127.0.0.1:$PORT"

echo "== 健康检查 =="
curl -s "$base/healthz"; echo

echo "== 首次扫描 =="
curl -s -X POST "$base/api/v1/scan" -H 'Content-Type: application/json' -d "{
  \"root\": \"$WORK/demo\",
  \"targets\": [\"app/main.c\"],
  \"system_include_dirs\": [\"sysinc\"],
  \"cache_dir\": \"$WORK/cache\"
}" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("status:", d["status"]); print("nodes:", len(d["graph"]["nodes"]), "edges:", d["graph"]["stats"]["edges_total"], "cycles:", d["graph"]["cycles"])'

echo "== 删除 version 相关引用前，先看受影响分析 =="
rm "$WORK/demo/app/common_impl.h"
curl -s -X POST "$base/api/v1/affected" -H 'Content-Type: application/json' -d "{
  \"root\": \"$WORK/demo\",
  \"targets\": [\"app/main.c\"],
  \"system_include_dirs\": [\"sysinc\"],
  \"cache_dir\": \"$WORK/cache\"
}" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("changed:", d["changed"]); print("affected_targets:", d["affected_targets"])'

echo "== 单文件解析（注释内伪 include 不应出现） =="
curl -s -X POST "$base/api/v1/parse" -H 'Content-Type: application/json' -d "{
  \"root\": \"$WORK/demo\",
  \"file\": \"app/common.h\"
}" | python3 -m json.tool
