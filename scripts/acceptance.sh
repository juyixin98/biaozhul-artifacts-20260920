#!/bin/sh
# 端到端验收:真实 gcc 编译、缓存命中、环境失效、产物隔离、重启保留。
# 用法: ./scripts/acceptance.sh
set -eu

cd "$(dirname "$0")/.."
PORT=$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')
ADDR="127.0.0.1:$PORT"
BASE="http://$ADDR"
TMP=$(mktemp -d)
DATA="$TMP/data"
WS="$TMP/ws"
mkdir -p "$WS"
cp -r examples "$WS/"
PID=""

cleanup() {
  [ -n "$PID" ] && kill "$PID" 2>/dev/null || true
  rm -rf "$TMP"
}
trap cleanup EXIT

say() { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }
field() { sed -n "s/.*\"$1\": \"\\([^\"]*\\)\".*/\\1/p" | head -1; }
check() { # check <描述> <文本> <期望子串>
  case "$2" in
    *"$3"*) echo "ok: $1" ;;
    *) echo "FAIL: $1 — 期望包含 [$3],实际:"; echo "$2"; exit 1 ;;
  esac
}

say "构建二进制"
go build -o "$TMP/cacheserver" ./cmd/cacheserver
go build -o "$TMP/cachectl" ./cmd/cachectl

say "启动服务 (数据目录 $DATA)"
"$TMP/cacheserver" -addr "$ADDR" -data "$DATA" -workspace "$WS" >"$TMP/server.log" 2>&1 &
PID=$!
sleep 1
CTL() { "$TMP/cachectl" -addr "$BASE" "$@"; }

say "第一次构建 v1 (应为 miss,真实 gcc 编译)"
OUT1=$(CTL build examples/hello/task.v1.json)
check "v1 miss" "$OUT1" '"cache": "miss"'
check "v1 输出 1.0.0" "$OUT1" 'app_version=1.0.0'
KEY1=$(echo "$OUT1" | field key)
DIG1=$(echo "$OUT1" | field digest)

say "再次构建 v1 (应为 hit,同一产物)"
OUT2=$(CTL build examples/hello/task.v1.json)
check "v1 hit" "$OUT2" '"cache": "hit"'
[ "$(echo "$OUT2" | field digest)" = "$DIG1" ] && echo "ok: 产物摘要一致" || { echo "FAIL: 产物摘要变化"; exit 1; }

say "声明环境变化 CPATH=include-v2 (应为 miss,键不同,输出 2.0.0)"
OUT3=$(CTL build examples/hello/task.v2.json)
check "v2 miss" "$OUT3" '"cache": "miss"'
check "v2 输出 2.0.0" "$OUT3" 'app_version=2.0.0'
[ "$(echo "$OUT3" | field key)" != "$KEY1" ] && echo "ok: 键已失效" || { echo "FAIL: 环境变化未改变键"; exit 1; }

say "遗漏环境声明 (不会命中旧结果;构建如实失败)"
if OUT4=$(CTL build examples/hello/task.noenv.json); then
  echo "FAIL: 未声明 CPATH 的构建应当失败"; echo "$OUT4"; exit 1
else
  echo "ok: 未声明环境 -> 构建失败(编译器真实报错),未命中旧缓存"
fi

say "审计端点 (键的完整输入)"
AUDIT=$(CTL audit "$KEY1")
check "审计含环境绑定" "$AUDIT" '"CPATH": "include-v1"'
check "审计含工具链摘要" "$AUDIT" '"binary_sha256"'
check "审计含源码清单" "$AUDIT" '"sha256"'

say "产物损坏 -> 隔离并重建"
echo garbage >> "$DATA/cas/$DIG1"
OUT5=$(CTL build examples/hello/task.v1.json)
check "损坏后重建 miss" "$OUT5" '"cache": "miss"'
[ -n "$(ls -A "$DATA/quarantine")" ] && echo "ok: 损坏产物已隔离" || { echo "FAIL: 隔离目录为空"; exit 1; }

say "产物丢失 -> 重建"
rm -f "$DATA/cas/$DIG1"
OUT6=$(CTL build examples/hello/task.v1.json)
check "丢失后重建 miss" "$OUT6" '"cache": "miss"'

say "重启服务 -> 缓存仍然命中"
kill "$PID"; PID=""
sleep 1
"$TMP/cacheserver" -addr "$ADDR" -data "$DATA" -workspace "$WS" >>"$TMP/server.log" 2>&1 &
PID=$!
sleep 1
OUT7=$(CTL build examples/hello/task.v1.json)
check "重启后 hit" "$OUT7" '"cache": "hit"'

say "修改源码 -> 缓存失效"
sed -i 's/hello from util/hello from util v2/' "$WS/examples/hello/util.c"
OUT8=$(CTL build examples/hello/task.v1.json)
check "源码修改后 miss" "$OUT8" '"cache": "miss"'
check "新输出" "$OUT8" 'hello from util v2'

say "并发同键 -> 只有一个发布者"
# 此时 util.c 已修改,task.v2 是一个从未构建过的新键
PIDS=""
for i in 1 2 3 4 5 6; do
  CTL build examples/hello/task.v2.json >"$TMP/conc.$i" 2>&1 &
  PIDS="$PIDS $!"
done
wait $PIDS
MISSES=$(grep -l '"cache": "miss"' "$TMP"/conc.* | wc -l)
[ "$MISSES" = "1" ] && echo "ok: 6 个并发请求中恰好 1 个发布者 (miss=1)" || { echo "FAIL: 并发发布者数量 = $MISSES,期望 1"; cat "$TMP"/conc.*; exit 1; }

say "全部验收通过"
