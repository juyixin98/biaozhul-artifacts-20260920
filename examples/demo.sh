#!/usr/bin/env bash
# 端到端 curl 演示：在独立数据目录启动服务，依次演示
#   正常提交 / 事务回滚 / 主键变更 / 重复位置幂等 / 缺口暂停 / 跨重启半事务 / 重放
# 用法: ./examples/demo.sh [port]
set -u
PORT="${1:-18090}"
BASE="http://localhost:${PORT}"
DIR="$(mktemp -d /tmp/cdc-demo.XXXXXX)"
cd "$(dirname "$0")/.."

post() { # post <json>
  curl -sS -X POST "$BASE/v1/events" -H 'Content-Type: application/json' -d "$1"
}
get() { curl -sS "$BASE$1"; }
show() { printf '\n===== %s =====\n' "$1"; }

JAVA_PID=""
start() {
  java -cp build/classes cdcrebuild.Main "$PORT" "$DIR" id >/tmp/cdc-demo.log 2>&1 &
  JAVA_PID=$!
  for i in $(seq 1 50); do
    curl -sS "$BASE/healthz" >/dev/null 2>&1 && return 0
    sleep 0.1
  done
  echo "服务启动失败（端口 $PORT 可能被占用），日志如下:"; cat /tmp/cdc-demo.log; exit 1
}
stop() { [ -n "$JAVA_PID" ] && kill "$JAVA_PID" 2>/dev/null && wait "$JAVA_PID" 2>/dev/null || true; }
restart() { stop; echo "(服务已停止，模拟进程退出；数据保留在 $DIR)"; start; echo "(服务已重启)"; }
trap stop EXIT

show "启动服务（数据目录 $DIR，端口 $PORT）"
start
get /healthz

show "场景 0：健康检查"
get /healthz

show "场景 1：正常提交事务（BEGIN/DATA/DATA/COMMIT，pos 1-4）"
post '{"pos":1,"txId":"tx-1","type":"BEGIN"}'
post '{"pos":2,"txId":"tx-1","type":"DATA","table":"users","op":"INSERT","new":{"id":1,"name":"alice"}}'
post '{"pos":3,"txId":"tx-1","type":"DATA","table":"users","op":"INSERT","new":{"id":2,"name":"bob"}}'
post '{"pos":4,"txId":"tx-1","type":"COMMIT"}'
echo; get /v1/tables/users

show "场景 2：事务回滚（pos 5-7，插入 id=3 后 ROLLBACK，数据必须消失）"
post '{"pos":5,"txId":"tx-2","type":"BEGIN"}'
post '{"pos":6,"txId":"tx-2","type":"DATA","table":"users","op":"INSERT","new":{"id":3,"name":"carol-会回滚"}}'
post '{"pos":7,"txId":"tx-2","type":"ROLLBACK"}'
echo; get /v1/tables/users
echo; get "/v1/log?from=1&to=7"

show "场景 3：主键变更（pos 8-10，id=1 -> id=10，旧键删除、新键生效）"
post '{"pos":8,"txId":"tx-3","type":"BEGIN"}'
post '{"pos":9,"txId":"tx-3","type":"DATA","table":"users","op":"UPDATE","old":{"id":1,"name":"alice"},"new":{"id":10,"name":"alice-renamed"},"pkChanged":true}'
post '{"pos":10,"txId":"tx-3","type":"COMMIT"}'
echo; echo "-- 新键 id=10 --"; get /v1/tables/users/row/10
echo; echo "-- 旧键 id=1（应为 404） --"; get /v1/tables/users/row/1

show "场景 4：重复位置幂等（重发 pos=10 的 COMMIT，返回 DUPLICATE，表不变）"
post '{"pos":10,"txId":"tx-3","type":"COMMIT"}'
echo; echo "-- 同位置不同内容（应为 409）--"
post '{"pos":9,"txId":"different","type":"COMMIT"}'

show "场景 5：缺口暂停（先投 pos=13，缺 12；返回 202 + PAUSED，绝不跳过）"
post '{"pos":11,"txId":"tx-4","type":"BEGIN"}'
post '{"pos":13,"txId":"tx-4","type":"DATA","table":"users","op":"INSERT","new":{"id":4,"name":"dave"}}'
echo; get /v1/status
show "场景 5b：补齐缺口 pos=12，缓冲的 13 自动排空"
post '{"pos":12,"txId":"tx-4","type":"DATA","table":"users","op":"INSERT","new":{"id":5,"name":"erin"}}'
post '{"pos":14,"txId":"tx-4","type":"COMMIT"}'
echo; get /v1/status
echo; get /v1/tables/users

show "场景 6：跨重启半事务（BEGIN+DATA 后杀进程，重启后不可见，补 COMMIT 生效）"
post '{"pos":15,"txId":"tx-5","type":"BEGIN"}'
post '{"pos":16,"txId":"tx-5","type":"DATA","table":"users","op":"INSERT","new":{"id":6,"name":"frank-半事务"}}'
restart
echo "-- 重启后状态：tx-5 仍 open，id=6 不可见 --"
get /v1/status
echo; get /v1/tables/users
echo "-- 重放：补 pos=17 COMMIT --"
post '{"pos":17,"txId":"tx-5","type":"COMMIT"}'
echo; get /v1/tables/users

show "场景 7：最终全量重放校验（审计日志与当前表）"
get /v1/tables
echo; get "/v1/log?from=1&to=17"

printf '\n演示完成，数据目录: %s\n' "$DIR"
