#!/usr/bin/env bash
# 端到端演示：启动真实 HTTP 服务，用 curl 覆盖
# 提交前不可见 / 回滚 / 主键变更 / 幂等 / 缺口暂停 / 跨重启半事务 / 重放对账。
set -euo pipefail
cd "$(dirname "$0")"
DATA="$(mktemp -d)"

./build.sh >/dev/null

# 端口可用性检查：能建立 TCP 连接即视为被占用，自动递增找空闲端口
PORT="${1:-18080}"
in_use() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }
while in_use "$PORT"; do
  echo "端口 $PORT 被占用，尝试 $((PORT+1)) ..."
  PORT=$((PORT+1))
done
BASE="http://127.0.0.1:${PORT}"
echo "使用端口: $PORT  数据目录: $DATA"

java -cp out com.example.cdc.Main "$PORT" "$DATA" >/tmp/cdc-demo.log 2>&1 &
PID=$!
cleanup() { kill "$PID" 2>/dev/null || true; rm -rf "$DATA"; }
trap cleanup EXIT

# 等端口就绪
for _ in $(seq 1 50); do
  curl -sf "$BASE/health" >/dev/null 2>&1 && break
  sleep 0.1
done

say() { printf '\n===== %s =====\n' "$1"; }
post() { curl -s -X POST "$BASE$1" -H 'Content-Type: application/json' -d "$2"; echo; }
get() { curl -s "$BASE$1"; echo; }

say "1) 事务 T1：INSERT + 改主键(id 10->11)，COMMIT 前查询不可见"
post /events '[
 {"position":1,"type":"DATA","txn":"T1","table":"users","op":"INSERT","pk":[10],
  "newValues":{"id":10,"name":"alice","city":"BJ"}},
 {"position":2,"type":"DATA","txn":"T1","table":"users","op":"UPDATE","pk":[11],"oldPk":[10],
  "newValues":{"id":11,"name":"alice","city":"SH"}}
]'
echo "-- 提交前 GET /tables/users（期望 []）--"
get /tables/users
echo "-- GET /txns/T1（期望 open=true, dataCount=2）--"
get /txns/T1

say "2) COMMIT T1，改主键整体生效（旧键消失，新键存在）"
post /events '[{"position":3,"type":"COMMIT","txn":"T1"}]'
get /tables/users

say "3) 事务 T2 回滚：INSERT 后 ROLLBACK，不留数据"
post /events '[
 {"position":4,"type":"DATA","txn":"T2","table":"users","op":"INSERT","pk":[12],
  "newValues":{"id":12,"name":"bob"}},
 {"position":5,"type":"ROLLBACK","txn":"T2"}
]'
get /tables/users

say "4) 幂等：重发位置 1..5，全部判重，状态不变"
post /events '[
 {"position":1,"type":"DATA","txn":"T1","table":"users","op":"INSERT","pk":[10],
  "newValues":{"id":10,"name":"alice","city":"BJ"}},
 {"position":3,"type":"COMMIT","txn":"T1"}
]'

say "5) 缺口暂停：直接发位置 8（缺 6/7），状态 PAUSED 且不生效"
post /events '[{"position":8,"type":"DATA","txn":"T3","table":"users","op":"INSERT","pk":[13],
 "newValues":{"id":13,"name":"carol"}}]'
get /status
get /tables/users

say "6) 补齐 6/7，位置 8 自动排空；T3 仍 OPEN，再发 COMMIT 9"
post /events '[
 {"position":6,"type":"DATA","txn":"T9","table":"orders","op":"INSERT","pk":["o1"],
  "newValues":{"order":"o1","amount":100}},
 {"position":7,"type":"COMMIT","txn":"T9"}
]'
get /status
post /events '[{"position":9,"type":"COMMIT","txn":"T3"}]'
get /tables/users
get /tables/orders

say "7) 跨重启半事务：T4 写两条 DATA 不提交 -> kill 服务 -> 重新启动 -> 仍 OPEN/不可见 -> COMMIT"
post /events '[
 {"position":10,"type":"DATA","txn":"T4","table":"users","op":"INSERT","pk":[20],
  "newValues":{"id":20,"name":"dave"}},
 {"position":11,"type":"DATA","txn":"T4","table":"users","op":"INSERT","pk":[21],
  "newValues":{"id":21,"name":"erin"}}
]'

# 干净停止旧实例：TERM 后等待进程真正退出，再在“新端口”上启动新实例
# （状态在数据目录、与端口无关；换新端口避免旧监听尚未释放时两个 JVM 短暂共存）
kill -TERM "$PID" 2>/dev/null || true
for _ in $(seq 1 50); do kill -0 "$PID" 2>/dev/null || break; sleep 0.1; done
kill -KILL "$PID" 2>/dev/null || true
for _ in $(seq 1 50); do in_use "$PORT" || break; sleep 0.1; done
OLDPORT=$PORT
while in_use $((PORT+1)); do PORT=$((PORT+1)); done
PORT=$((PORT+1))
BASE="http://127.0.0.1:${PORT}"

: > /tmp/cdc-demo.log
java -cp out com.example.cdc.Main "$PORT" "$DATA" >/tmp/cdc-demo.log 2>&1 &
PID=$!
for _ in $(seq 1 50); do curl -sf "$BASE/health" >/dev/null 2>&1 && break; sleep 0.1; done
echo "（实例在端口 $OLDPORT 停止，新实例端口 $PORT；数据目录同为 $DATA）"
echo "-- 重启后 /txns/T4（open=true）与 /tables/users（看不到 T4）--"
get /txns/T4
get /tables/users
post /events '[{"position":12,"type":"COMMIT","txn":"T4"}]'
get /tables/users

say "8) 重放对账：最终表状态必须与源事务解释器一致（match=true）"
get /reconcile
get /changes?table=users

say "完成。数据目录: $DATA（脚本退出时自动清理）"
