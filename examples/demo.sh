#!/usr/bin/env bash
# 演示：缓存未命中 -> 重新执行 -> 上传输出 -> 发布 -> 缓存命中 -> 下载校验
# 以及：缺块发布被拒、缓存对象损坏不得伪成功。
# 用法：先启动服务（见 README），然后 ./examples/demo.sh
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:8080}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

say() { printf '\n=== %s ===\n' "$*"; }

# 模拟一次“构建动作”：动作摘要 = 命令 + 输入内容的 sha256。
COMMAND="gcc -c hello.c"
INPUT="int main(){return 0;} // v1"
ACTION_DIGEST=$(printf '%s' "$COMMAND$INPUT" | sha256sum | cut -d' ' -f1)

say "1. 查询动作缓存（预期 404 未命中）"
curl -s -o /dev/null -w 'GET /ac/%{http_code}\n' "$BASE/ac/$ACTION_DIGEST"

say "2. 缓存未命中 -> 本地重新执行"
OUTPUT="hello.o (fake object code for: $INPUT)"
echo "$OUTPUT" > "$WORK/hello.o"
OUT_DIGEST=$(sha256sum "$WORK/hello.o" | cut -d' ' -f1)
OUT_SIZE=$(stat -c%s "$WORK/hello.o")
echo "产出 hello.o  sha256=$OUT_DIGEST"

say "3. 上传输出块到 CAS（内容寻址，服务端校验摘要）"
curl -s -o /dev/null -w 'PUT /cas/%{http_code}\n' \
  -X PUT --data-binary @"$WORK/hello.o" "$BASE/cas/$OUT_DIGEST"

say "4. 缺块发布（引用未上传对象，预期 422 且不落 AC）"
MISSING_DIGEST=$(printf 'never uploaded' | sha256sum | cut -d' ' -f1)
BAD_ACTION=$(printf 'bad action' | sha256sum | cut -d' ' -f1)
curl -s -w '\nPUT /ac -> %{http_code}\n' -X PUT \
  -H 'content-type: application/json' \
  -d "{\"exit_code\":0,\"outputs\":[{\"path\":\"ghost.o\",\"digest\":\"$MISSING_DIGEST\",\"size\":14}]}" \
  "$BASE/ac/$BAD_ACTION"
curl -s -o /dev/null -w 'GET /ac(bad) -> %{http_code} (仍应 404)\n' "$BASE/ac/$BAD_ACTION"

say "5. 全部对象核验通过 -> 发布动作结果（预期 201）"
curl -s -w '\nPUT /ac -> %{http_code}\n' -X PUT \
  -H 'content-type: application/json' \
  -d "{\"exit_code\":0,\"outputs\":[{\"path\":\"hello.o\",\"digest\":\"$OUT_DIGEST\",\"size\":$OUT_SIZE}]}" \
  "$BASE/ac/$ACTION_DIGEST"

say "6. 再次查询同一动作（预期 200 缓存命中）"
curl -s -w '\nGET /ac -> %{http_code}\n' "$BASE/ac/$ACTION_DIGEST"

say "7. 按清单下载输出并本地校验摘要"
curl -s "$BASE/cas/$OUT_DIGEST" -o "$WORK/downloaded.o"
DOWN_DIGEST=$(sha256sum "$WORK/downloaded.o" | cut -d' ' -f1)
if [ "$DOWN_DIGEST" = "$OUT_DIGEST" ]; then
  echo "摘要一致：$DOWN_DIGEST  ✅"
else
  echo "摘要不一致！期望 $OUT_DIGEST 实得 $DOWN_DIGEST  ❌" >&2
  exit 1
fi

say "8. 模拟缓存对象损坏 -> 下载与命中必须报错（预期 500，不得伪成功）"
DATA_DIR="${DATA_DIR:-./data}"
CAS_FILE="$DATA_DIR/cas/${OUT_DIGEST:0:2}/$OUT_DIGEST"
if [ -f "$CAS_FILE" ]; then
  cp "$CAS_FILE" "$WORK/backup"
  printf 'tampered' > "$CAS_FILE"
  curl -s -o /dev/null -w 'GET /cas(损坏) -> %{http_code}\n' "$BASE/cas/$OUT_DIGEST"
  curl -s -o /dev/null -w 'GET /ac(引用损坏对象) -> %{http_code}\n' "$BASE/ac/$ACTION_DIGEST"
  cp "$WORK/backup" "$CAS_FILE"
  curl -s -o /dev/null -w '恢复后 GET /cas -> %{http_code}\n' "$BASE/cas/$OUT_DIGEST"
else
  echo "跳过：找不到数据目录 $DATA_DIR（用 DATA_DIR=... 指定服务端数据目录可启用此步骤）"
fi

say "演示完成"
