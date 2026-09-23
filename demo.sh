#!/usr/bin/env bash
# 端到端演示三个验收场景（需要系统自带 curl）：
#   1) A,A,B,C        -> 2 个匹配
#   2) A,B,超时,C     -> 0 个匹配
#   3) 硬崩溃后重启   -> 已完成匹配与部分匹配状态一致，并可继续匹配
#
# 用法: ./demo.sh
# 脚本会在本机随机挑选空闲端口启动服务，结束时清理进程与临时数据。
set -euo pipefail
cd "$(dirname "$0")"

if [[ -n "${JAVA_HOME:-}" ]]; then
  JAVA="$JAVA_HOME/bin/java"
else
  JAVA="$(command -v java)"
fi
if ! command -v curl >/dev/null; then
  echo "错误: 演示脚本需要 curl" >&2
  exit 1
fi
if [[ ! -d build/classes ]]; then
  ./build.sh
fi

PORT="$(python3 - <<'PY' 2>/dev/null || sh -c 'echo 18080'
import socket
s = socket.socket()
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
s.close()
PY
)"
DATA_DIR="$(mktemp -d)"
BASE="http://127.0.0.1:$PORT"
PID=""

cleanup() {
  if [[ -n "$PID" ]] && kill -0 "$PID" 2>/dev/null; then
    kill "$PID" 2>/dev/null || true
    wait "$PID" 2>/dev/null || true
  fi
  rm -rf "$DATA_DIR"
}
trap cleanup EXIT

post() {
  local path="$1" body="$2"
  echo "\$ curl -sS -X POST $BASE$path -H 'Content-Type: application/json' -d '$body'"
  curl -sS -X POST "$BASE$path" -H 'Content-Type: application/json' -d "$body"
  echo
}
get() {
  local path="$1"
  echo "\$ curl -sS $BASE$path"
  curl -sS "$BASE$path"
  echo
}

echo "== 启动服务（开启测试端点）数据目录: $DATA_DIR =="
"$JAVA" -cp build/classes cep.Main \
  --port="$PORT" --data-dir="$DATA_DIR" --test-endpoints &
PID=$!

echo "等待端口就绪..."
for _ in $(seq 1 100); do
  if curl -fsS "$BASE/health" >/dev/null 2>&1; then break; fi
  sleep 0.1
done

echo
echo "########## 验收 1: A,A,B,C -> 期望 2 个匹配 ##########"
post /events '[{"type":"A","entityId":"e1","timestamp":1000},
              {"type":"A","entityId":"e1","timestamp":1000},
              {"type":"B","entityId":"e1","timestamp":2000},
              {"type":"C","entityId":"e1","timestamp":3000}]'
get /matches?entityId=e1

echo
echo "########## 验收 2: A,B,超时(>10s),C -> 期望 0 个匹配 ##########"
post /events '[{"type":"A","entityId":"e2","timestamp":0},
              {"type":"B","entityId":"e2","timestamp":1000}]'
post /events '[{"type":"C","entityId":"e2","timestamp":11000}]'
get /state?entityId=e2
get /matches

echo
echo "########## 验收 3: 硬崩溃 -> 重启 -> 状态一致 ##########"
echo "-- 崩溃前留下一个未完成的部分匹配 A,B（实体 e3）--"
post /events '[{"type":"A","entityId":"e3","timestamp":50000},
              {"type":"B","entityId":"e3","timestamp":50500}]'
get /state?entityId=e3
echo "-- 触发硬崩溃（Runtime.halt，不执行优雅退出）--"
post /test/crash '{}'
for _ in $(seq 1 50); do
  if ! kill -0 "$PID" 2>/dev/null; then break; fi
  sleep 0.1
done
echo "-- 旧进程已退出（exit code 见下）--"
wait "$PID" && true
echo "旧进程退出码: $?"
PID=""

echo "-- 用同一数据目录重新启动 --"
"$JAVA" -cp build/classes cep.Main \
  --port="$PORT" --data-dir="$DATA_DIR" --test-endpoints &
PID=$!
for _ in $(seq 1 100); do
  if curl -fsS "$BASE/health" >/dev/null 2>&1; then break; fi
  sleep 0.1
done

echo "-- 重启后查询：已完成匹配仍是 2；e2 超时未误配 --"
get /matches
echo "-- e3 的部分匹配 A->B 必须仍在 --"
get /state?entityId=e3
echo "-- 给 e3 补 C（窗口内），恢复后的部分匹配成功命中 --"
post /events '[{"type":"C","entityId":"e3","timestamp":50900}]'
get /matches?entityId=e3

echo
echo "########## 附加: 相同时间戳按输入序号排序 ##########"
post /events '[{"type":"B","entityId":"e4","timestamp":70000},
              {"type":"A","entityId":"e4","timestamp":70000},
              {"type":"C","entityId":"e4","timestamp":70000}]'
get /matches?entityId=e4

echo
echo "########## 附加: 组合超上限显式 422（不静默截断）##########"
echo "-- 用 --max-combines=1 重启服务，再发 A,A,B,C（单个 C 有 2 个候选）--"
kill "$PID" 2>/dev/null || true
wait "$PID" 2>/dev/null || true
PID=""
"$JAVA" -cp build/classes cep.Main \
  --port="$PORT" --data-dir="$DATA_DIR" --max-combines=1 &
PID=$!
for _ in $(seq 1 100); do
  if curl -fsS "$BASE/health" >/dev/null 2>&1; then break; fi
  sleep 0.1
done
post /events '[{"type":"A","entityId":"cap","timestamp":0},
              {"type":"A","entityId":"cap","timestamp":1},
              {"type":"B","entityId":"cap","timestamp":2},
              {"type":"C","entityId":"cap","timestamp":3}]'
echo "-- 该批次整体未生效：匹配总数不增加 --"
get /matches?entityId=cap

echo
echo "演示结束。"
