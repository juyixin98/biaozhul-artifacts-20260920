#!/usr/bin/env bash
# requests.sh — 端到端演示：启动本地服务、上传两个制品、生成补丁、
# 预置旧制品、原子应用、核验摘要、重复应用（幂等）、错基线拒绝。
#
# 仅依赖：bash、curl、go、sha256sum。不访问任何网络服务（除本机回环）。
set -euo pipefail

# 选择一个回环上空闲端口（可用 python3 时）；否则用固定端口并允许 ADDR 覆盖
pick_port() {
  if command -v python3 >/dev/null 2>&1; then
    python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()'
  else
    echo 18080
  fi
}
ADDR="${ADDR:-127.0.0.1:$(pick_port)}"
ROOT="$(mktemp -d)"
CACHE="$ROOT/cache"
WORK="$ROOT/work"
echo "== 临时目录: $ROOT"

cleanup() {
  if [[ -n "${SRV_PID:-}" ]] && kill -0 "$SRV_PID" 2>/dev/null; then
    kill "$SRV_PID" 2>/dev/null || true
    wait "$SRV_PID" 2>/dev/null || true
  fi
  rm -rf "$ROOT"
}
trap cleanup EXIT

BIN="$ROOT/delta-update"
go build -o "$BIN" ./cmd/delta-update

echo "== 启动本地服务 $ADDR"
"$BIN" serve --addr "$ADDR" --cache "$CACHE" --work "$WORK" &
SRV_PID=$!

# 等待端口就绪
for _ in $(seq 1 50); do
  if curl -fsS "http://$ADDR/v1/health" >/dev/null 2>&1; then break; fi
  sleep 0.1
done

base="http://$ADDR"

echo
echo "== 1) 健康检查"
curl -fsS "$base/v1/health"; echo

# 构造旧制品（约 72KB 重复内容）与新制品（开头附近插入一段，导致所有块位移）
head -c 73728 /dev/zero | tr '\0' 'O' > "$ROOT/old.bin"
{ head -c 123 /dev/zero | tr '\0' 'O'
  printf '>>>INSERTED-REGION<<<'
  tail -c +124 "$ROOT/old.bin"
  printf '\nTAIL\n'; } > "$ROOT/new.bin"

OLD_SUM=$(sha256sum "$ROOT/old.bin" | cut -d' ' -f1)
NEW_SUM=$(sha256sum "$ROOT/new.bin" | cut -d' ' -f1)
echo
echo "旧摘要: $OLD_SUM"
echo "新摘要: $NEW_SUM"

echo
echo "== 2) 上传旧/新制品到缓存"
curl -fsS -X POST --data-binary @"$ROOT/old.bin" \
  -H 'Content-Type: application/octet-stream' "$base/v1/artifacts"; echo
curl -fsS -X POST --data-binary @"$ROOT/new.bin" \
  -H 'Content-Type: application/octet-stream' "$base/v1/artifacts"; echo

echo
echo "== 3) 生成块级差量补丁"
PATCH_JSON="$ROOT/patch.json"
# 响应为 {"patch":{...}}，剥掉外层包装后保存补丁对象
curl -fsS -X POST "$base/v1/deltas" -H 'Content-Type: application/json' \
  -d "{\"oldRef\":\"$OLD_SUM\",\"newRef\":\"$NEW_SUM\",\"blockSize\":1024}" \
  | sed -E 's/^\{"patch":(\{.*)\}$/\1/' > "$PATCH_JSON"
echo "补丁已保存: $PATCH_JSON ($(wc -c < "$PATCH_JSON") bytes)"
echo "copy/data 操作数:"
grep -o '"type":"copy"' "$PATCH_JSON" | wc -l
grep -o '"type":"data"' "$PATCH_JSON" | wc -l

echo
echo "== 4) 在工作目录预置旧制品 builds/app1/current.bin"
curl -fsS -X PUT --data-binary @"$ROOT/old.bin" \
  -H 'Content-Type: application/octet-stream' \
  "$base/v1/work/builds/app1/current.bin"; echo

echo
echo "== 5) 原子应用补丁"
APPLY_BODY="$ROOT/apply.json"
printf '{"target":"builds/app1/current.bin","patch":%s}' \
  "$(cat "$PATCH_JSON")" > "$APPLY_BODY"
curl -fsS -X POST -H 'Content-Type: application/json' \
  --data-binary @"$APPLY_BODY" "$base/v1/apply"; echo

echo
echo "== 6) 核验最终摘要"
INFO=$(curl -fsS "$base/v1/work/builds/app1/current.bin")
echo "$INFO"
echo "$INFO" | grep -q "$NEW_SUM" && echo "PASS: 最终摘要 == newSum" || { echo "FAIL"; exit 1; }

echo
echo "== 7) 再次应用（幂等：applied=false）"
curl -fsS -X POST -H 'Content-Type: application/json' \
  --data-binary @"$APPLY_BODY" "$base/v1/apply"; echo

echo
echo "== 8) 错基线拒绝（预置不同内容后应用同一补丁，期望 409）"
head -c 73728 /dev/zero | tr '\0' 'X' > "$ROOT/other.bin"
curl -fsS -X PUT --data-binary @"$ROOT/other.bin" \
  "$base/v1/work/builds/app1/current.bin" >/dev/null
HTTP_CODE=$(curl -s -o "$ROOT/err.json" -w '%{http_code}' \
  -X POST -H 'Content-Type: application/json' \
  --data-binary @"$APPLY_BODY" "$base/v1/apply")
echo "HTTP $HTTP_CODE: $(cat "$ROOT/err.json")"
[[ "$HTTP_CODE" == "409" ]] && echo "PASS: 错基线被拒绝" || { echo "FAIL"; exit 1; }

echo
echo "== 9) 空间不足预检（simFreeBytes=1，期望 507）"
# 恢复为旧制品以便应用
curl -fsS -X PUT --data-binary @"$ROOT/old.bin" \
  "$base/v1/work/builds/app1/current.bin" >/dev/null
SPACE_BODY="$ROOT/space.json"
printf '{"target":"builds/app1/current.bin","patch":%s,"simFreeBytes":1}' \
  "$(cat "$PATCH_JSON")" > "$SPACE_BODY"
HTTP_CODE=$(curl -s -o "$ROOT/space.resp" -w '%{http_code}' \
  -X POST -H 'Content-Type: application/json' \
  --data-binary @"$SPACE_BODY" "$base/v1/apply")
echo "HTTP $HTTP_CODE: $(cat "$ROOT/space.resp")"
[[ "$HTTP_CODE" == "507" ]] && echo "PASS: 空间不足被预检拦截" || { echo "FAIL"; exit 1; }

echo
echo "全部演示通过。"
