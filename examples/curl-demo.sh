#!/usr/bin/env bash
# 端到端 curl 演示：
#   1) 造一个 ~10 MiB 的随机文件，切成 4 MiB 块
#   2) 创建会话（声明整体哈希/长度/每块大小）
#   3) 乱序上传，中途演示“缺块时 finish 被拒”
#   4) 演示同内容重传幂等、异内容拒绝
#   5) 完成发布，发布前 404 / 发布后 200 校验
# 依赖：bash、curl、coreutils(sha256sum/split/stat/mktemp)、python3（拼 JSON，避免依赖 jq）
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:8080}"
CHUNK="${CHUNK:-$((4 * 1024 * 1024))}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

req() { curl -sS -w '\n__HTTP__%{http_code}\n' "$@"; }
code() { tail -n1 <<<"$1" | sed 's/__HTTP__//'; }
body() { sed '$d' <<<"$1"; }

echo "== 0) 健康检查"
req "$BASE/healthz" | sed '$d'

echo "== 1) 生成 10 MiB 测试文件并切块（$CHUNK 字节/块）"
head -c $((10 * 1024 * 1024 + 123)) /dev/urandom > "$WORK/video.bin"
split -b "$CHUNK" -d "$WORK/video.bin" "$WORK/chunk-"
mapfile -t PARTS < <(find "$WORK" -name 'chunk-*' | sort)
WHOLE_SHA="$(sha256sum "$WORK/video.bin" | cut -d' ' -f1)"
TOTAL="$(stat -c%s "$WORK/video.bin")"
SIZES_JSON="$(for f in "${PARTS[@]}"; do stat -c%s "$f"; done | python3 -c 'import sys,json; print(json.dumps([int(x) for x in sys.stdin]))')"
echo "文件哈希: $WHOLE_SHA  长度: $TOTAL  块数: ${#PARTS[@]}"

echo "== 2) 创建上传会话"
R="$(req -XPOST "$BASE/uploads" -H 'content-type: application/json' \
  -d "{\"sha256\":\"$WHOLE_SHA\",\"total_size\":$TOTAL,\"chunk_sizes\":$SIZES_JSON}")"
echo "HTTP $(code "$R")"; body "$R"
UID_="$(body "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin)["upload_id"])')"

echo "== 3) 缺块时 finish -> 期望 400（一块都还没传）"
set +e
R="$(req -XPOST "$BASE/uploads/$UID_/finish")"
set -e
echo "HTTP $(code "$R"): $(body "$R")"

echo "== 4) 乱序上传（顺序: 最后一块 -> 第一块 ...），期望 201"
ORDER=($(seq $((${#PARTS[@]} - 1)) -1 0))
for i in "${ORDER[@]}"; do
  f="${PARTS[$i]}"
  sha="$(sha256sum "$f" | cut -d' ' -f1)"
  R="$(req -XPUT "$BASE/uploads/$UID_/chunks/$i?sha256=$sha" \
      -H 'content-type: application/octet-stream' --data-binary @"$f")"
  echo "chunk $i -> HTTP $(code "$R"): $(body "$R")"
done

echo "== 5) 同内容重传 chunk0 -> 期望 200 幂等"
f="${PARTS[0]}"; sha="$(sha256sum "$f" | cut -d' ' -f1)"
R="$(req -XPUT "$BASE/uploads/$UID_/chunks/0?sha256=$sha" --data-binary @"$f")"
echo "HTTP $(code "$R"): $(body "$R")"

echo "== 6) 异内容重传 chunk0（用 chunk1 的内容冒充）-> 期望 409"
f="${PARTS[1]}"; sha="$(sha256sum "$f" | cut -d' ' -f1)"
set +e
R="$(req -XPUT "$BASE/uploads/$UID_/chunks/0?sha256=$sha" --data-binary @"$f")"
set -e
echo "HTTP $(code "$R"): $(body "$R")"

echo "== 7) 发布前读取对象 -> 期望 404"
set +e
R="$(req "$BASE/objects/$WHOLE_SHA")"
set -e
echo "HTTP $(code "$R")"

echo "== 8) 完成发布 -> 200"
R="$(req -XPOST "$BASE/uploads/$UID_/finish")"
echo "HTTP $(code "$R"): $(body "$R")"

echo "== 9) 重复 finish -> 期望 200 + idempotent=true"
R="$(req -XPOST "$BASE/uploads/$UID_/finish")"
echo "HTTP $(code "$R"): $(body "$R")"

echo "== 10) 下载并逐字节校验"
curl -sS "$BASE/objects/$WHOLE_SHA" -o "$WORK/out.bin"
echo "下载长度: $(stat -c%s "$WORK/out.bin")（期望 $TOTAL）"
OUT_SHA="$(sha256sum "$WORK/out.bin" | cut -d' ' -f1)"
[ "$OUT_SHA" = "$WHOLE_SHA" ] && echo "SHA256 一致 ✔" || { echo "SHA256 不一致 ✗"; exit 1; }

echo "== 11) 整体哈希伪造会话 -> finish 期望 409，且对象不可读"
R="$(req -XPOST "$BASE/uploads" -H 'content-type: application/json' \
  -d "{\"sha256\":\"$(printf 'a%.0s' {1..64})\",\"total_size\":$TOTAL,\"chunk_sizes\":$SIZES_JSON}")"
B2="$(body "$R")"
UID2="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["upload_id"])' <<<"$B2")"
for i in "${!PARTS[@]}"; do
  f="${PARTS[$i]}"; sha="$(sha256sum "$f" | cut -d' ' -f1)"
  req -XPUT "$BASE/uploads/$UID2/chunks/$i?sha256=$sha" --data-binary @"$f" >/dev/null
done
set +e
R="$(req -XPOST "$BASE/uploads/$UID2/finish")"
set -e
echo "finish HTTP $(code "$R"): $(body "$R")"

set +e
R="$(req "$BASE/objects/$(printf 'a%.0s' {1..64})")"
set -e
echo "读取伪造哈希对象 -> HTTP $(code "$R")（期望 404，未发布不可读）"

echo
echo "全部演示完成。"
