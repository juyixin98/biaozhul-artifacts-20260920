#!/usr/bin/env bash
# 远端构建缓存原型 —— curl 请求样例脚本
#
# 用法：
#   1) 先启动服务：
#        cargo run --release --bin rbc-server -- --addr 127.0.0.1:8080 --store ./cache-data
#   2) 再运行本脚本（BASE 可覆盖）：
#        bash examples/requests.sh
#        BASE=http://127.0.0.1:9000 bash examples/requests.sh
#
# 需要：curl、jq、sha256sum、stat、coreutils。
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:8080}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

say() { printf '\n=== %s ===\n' "$*"; }

say "健康检查"
curl -sS "$BASE/healthz"

say "1) 构造动作并取规范摘要"
ACTION='{"arguments":["gcc","-c","main.c","-o","main.o"],
         "platform":{"cpu":"x86_64","os":"linux"},
         "working_directory":"."}'
AH="$(curl -sS -X POST "$BASE/util/action-digest" \
        -H 'content-type: application/json' -d "$ACTION" | jq -r .hash)"
echo "action key = $AH"

say "2) 发布前查询 -> 期望 404 cache_miss（重新执行路径）"
set +e
curl -sS -o "$WORK/miss.json" -w 'HTTP %{http_code}\n' "$BASE/actions/$AH"
set -e
jq -c . "$WORK/miss.json"

say "3) 重新执行后上传输出块"
printf '<<< fake main.o bytes >>>' > "$WORK/main.o"
BH="$(sha256sum "$WORK/main.o" | cut -d' ' -f1)"
N="$(stat -c%s "$WORK/main.o")"
echo "blob digest = $BH ($N bytes)"
curl -sS -X PUT "$BASE/blobs/$BH?size_bytes=$N" --data-binary @"$WORK/main.o" | jq -c .

say "4) find-missing（块已上传，应为空；另一个不存在的块应报缺失）"
GHOST="$(printf 'never uploaded' | sha256sum | cut -d' ' -f1)"
curl -sS -X POST "$BASE/find-missing" -H 'content-type: application/json' \
  -d "{\"digests\":[{\"hash\":\"$BH\",\"size_bytes\":$N},
                    {\"hash\":\"$GHOST\",\"size_bytes\":15}]}" | jq -c .

say "5) 缺块发布 -> 期望 424 missing_blobs，且不产生记录"
BAD_ACTION='{"arguments":["false"],"working_directory":"."}'
BAD_AH="$(curl -sS -X POST "$BASE/util/action-digest" \
           -H 'content-type: application/json' -d "$BAD_ACTION" | jq -r .hash)"
set +e
curl -sS -o "$WORK/bad.json" -w 'HTTP %{http_code}\n' -X PUT "$BASE/actions/$BAD_AH" \
  -H 'content-type: application/json' \
  -d "{\"action\":$BAD_ACTION,
       \"action_result\":{\"exit_code\":1,
         \"output_files\":[{\"path\":\"g\",\"digest\":{\"hash\":\"$GHOST\",\"size_bytes\":15},
                           \"is_executable\":false}]}}"
set -e
jq -c . "$WORK/bad.json"
echo "-- 随后 GET 仍是 404 cache_miss --"
set +e
curl -sS -o /dev/null -w 'HTTP %{http_code}\n' "$BASE/actions/$BAD_AH"
set -e

say "6) 全部对象核验通过后发布 -> 200 published"
curl -sS -X PUT "$BASE/actions/$AH" -H 'content-type: application/json' \
  -d "{\"action\":$ACTION,
       \"action_result\":{
         \"exit_code\":0,
         \"stdout_raw\":\"build ok\n\",
         \"output_files\":[{\"path\":\"main.o\",
                           \"digest\":{\"hash\":\"$BH\",\"size_bytes\":$N},
                           \"is_executable\":false}]}}" | jq -c .

say "7) 再次查询 -> 200 HIT（命中前服务端复验全部引用对象）"
curl -sS "$BASE/actions/$AH" | jq -c '{status, files: .action_result.output_files}'

say "8) 下载输出块并本地核对摘要（下载验证摘要）"
curl -sS "$BASE/blobs/$BH" -o "$WORK/got.o"
echo "downloaded sha256 = $(sha256sum "$WORK/got.o" | cut -d' ' -f1)"
echo "expected          = $BH"
[ "$(sha256sum "$WORK/got.o" | cut -d' ' -f1)" = "$BH" ] && echo "MATCH"

say "9) 哈希不符的上传 -> 期望 400"
WRONG="$(printf 'zzz' | sha256sum | cut -d' ' -f1)"
set +e
curl -sS -w '\nHTTP %{http_code}\n' -X PUT "$BASE/blobs/$WRONG?size_bytes=4" --data-binary abcd
set -e

say "10) 模拟磁盘损坏 -> GET blob / GET action 均为 503，HEAD 为 404；重新上传可自愈"
echo "（本脚本不改动服务端数据目录；请在服务端机器上按 README 手动演示）"
echo "    f=<store>/cas/${BH:0:2}/${BH:2:2}/$BH"
echo "    printf CORRUPT > \"\$f\"; curl -i $BASE/blobs/$BH; curl -i $BASE/actions/$AH"
