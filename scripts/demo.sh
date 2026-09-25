#!/usr/bin/env bash
# 端到端演示：启动服务 -> 摄入/查询 -> 重启验证 WAL 重放。
# 所有输出同时打到终端与 RUNLOG 的原始输出文件。
# 用法: scripts/demo.sh
set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# 动态挑选空闲 TCP 端口（允许通过 PORT 覆盖；默认自动探测，避免与机器上其他服务冲突）。
pick_port() {
  if command -v python3 >/dev/null 2>&1; then
    python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()'
  else
    echo 18080
  fi
}
PORT="${PORT:-$(pick_port)}"
BASE_URL="http://127.0.0.1:${PORT}"
DATA_DIR="$(mktemp -d -t counterreset-demo.XXXXXX)"
WAL="$DATA_DIR/counter.wal.jsonl"
trap 'kill "${SERVER_PID:-}" 2>/dev/null || true' EXIT

say() { printf '\n===== %s =====\n' "$1"; }

say "1. 编译"
go build -o "$DATA_DIR/counterreset" ./cmd/counterreset

say "2. 启动服务（空数据目录，首次启动会加载合成种子）"
"$DATA_DIR/counterreset" -addr=127.0.0.1:$PORT -data-dir="$DATA_DIR" -seed=examples/seed.jsonl &
SERVER_PID=$!

# 等待健康检查就绪（最多 ~10s）。
for _ in $(seq 1 100); do
  if curl -fsS "$BASE_URL/healthz" >/dev/null 2>&1; then break; fi
  sleep 0.1
done
curl -fsS "$BASE_URL/healthz"; echo

say "3. 序列列表（种子含 2 条序列）"
curl -fsS "$BASE_URL/v1/series"

say "4. 查询主序列完整窗口 [t0,t100]，不外推（下界点估计；upper=null 表示无上界信息）"
curl -fsS "$BASE_URL/v1/increase?label=__name__=http_requests_total&label=instance=demo-1&start_ms=1700000000000&end_ms=1700000100000&extrapolation=none"

say "5. 同窗口给定容量 C=100（条件上界，仅在“每区间至多一次重置”假设下成立）"
curl -fsS "$BASE_URL/v1/increase?label=__name__=http_requests_total&label=instance=demo-1&start_ms=1700000000000&end_ms=1700000100000&extrapolation=none&capacity=100"

say "6. 窗口 [t0,t200]：后 100s 无样本 —— none 不外推，coverage=0.5"
curl -fsS "$BASE_URL/v1/increase?label=__name__=http_requests_total&label=instance=demo-1&start_ms=1700000000000&end_ms=1700000200000&extrapolation=none"

say "7. 同窗口 linear 线性外推（启发式，非精确值）"
curl -fsS "$BASE_URL/v1/increase?label=__name__=http_requests_total&label=instance=demo-1&start_ms=1700000000000&end_ms=1700000200000&extrapolation=linear"

say "8. 同窗口 clamped 截断外推（默认策略，近似 Prometheus）"
curl -fsS "$BASE_URL/v1/rate?label=__name__=http_requests_total&label=instance=demo-1&start_ms=1700000000000&end_ms=1700000200000&extrapolation=clamped"

say "9. 乱序摄入新序列（一次性 POST 乱序样本）"
curl -fsS -X POST "$BASE_URL/v1/ingest" -H 'Content-Type: application/json' -d '{
  "labels": {"__name__":"manual_demo","env":"test"},
  "samples": [
    {"ts_ms":1700000030000,"value":30},
    {"ts_ms":1700000010000,"value":10},
    {"ts_ms":1700000020000,"value":20},
    {"ts_ms":1700000020000,"value":20},
    {"ts_ms":1700000020000,"value":99}
  ]
}'

say "10. 查询乱序序列（ts=20000 冲突按 max-wins 保留 99）"
curl -fsS "$BASE_URL/v1/increase?labels=__name__=manual_demo,env=test&start_ms=1700000010000&end_ms=1700000030000&extrapolation=none"

say "11. 负值样本被拒绝（整批拒绝，HTTP 400，无任何写入）"
curl -sS -o /tmp/counterreset_neg_resp.json -w 'HTTP %{http_code}\n' -X POST "$BASE_URL/v1/ingest" \
  -H 'Content-Type: application/json' \
  -d '{"labels":{"__name__":"bad"},"samples":[{"ts_ms":1700000010000,"value":5},{"ts_ms":1700000020000,"value":-1}]}'
cat /tmp/counterreset_neg_resp.json

say "12. NaN 也被拒绝（JSON 不支持 NaN 字面量，这里用 Go 测试覆盖；此处演示非法 JSON）"
curl -sS -o /tmp/counterreset_bad_resp.json -w 'HTTP %{http_code}\n' -X POST "$BASE_URL/v1/ingest" \
  -H 'Content-Type: application/json' -d '{not-json'
cat /tmp/counterreset_bad_resp.json

say "12b. 使用仓库中的请求样例文件摄入（--data @examples/ingest-reset.json）"
curl -fsS -X POST "$BASE_URL/v1/ingest" -H 'Content-Type: application/json' \
  --data @examples/ingest-reset.json
echo "--- 再用样例文件验证负值整批拒绝（--data @examples/ingest-negative-rejected.json）---"
curl -sS -o /tmp/counterreset_negfile_resp.json -w 'HTTP %{http_code}\n' \
  -X POST "$BASE_URL/v1/ingest" -H 'Content-Type: application/json' \
  --data @examples/ingest-negative-rejected.json
cat /tmp/counterreset_negfile_resp.json
echo "--- 查询样例文件写入的序列（t=10..40：13 + 3(重置 25→3) + 6 = 22）---"
curl -fsS "$BASE_URL/v1/increase?labels=__name__=http_requests_total,instance=demo-1,path=/api/v1/widgets&start_ms=1700000010000&end_ms=1700000040000&extrapolation=none"

say "13. 停止服务并重启（验证 WAL 持久化与重放）"
kill "$SERVER_PID"; wait "$SERVER_PID" 2>/dev/null || true
echo "--- WAL 文件内容（每行一条摄入记录，含原始乱序）---"
cat "$WAL"
"$DATA_DIR/counterreset" -addr=127.0.0.1:$PORT -data-dir="$DATA_DIR" -seed=examples/seed.jsonl &
SERVER_PID=$!
for _ in $(seq 1 100); do
  if curl -fsS "$BASE_URL/healthz" >/dev/null 2>&1; then break; fi
  sleep 0.1
done
echo "--- 重启后查询手动摄入的序列，数据仍在且已排序 ---"
curl -fsS "$BASE_URL/v1/increase?labels=__name__=manual_demo,env=test&start_ms=1700000010000&end_ms=1700000030000&extrapolation=none"

say "14. 不存在的序列 -> 404"
curl -sS -o /tmp/counterreset_404.json -w 'HTTP %{http_code}\n' \
  "$BASE_URL/v1/increase?label=__name__=missing&start_ms=1&end_ms=2"
cat /tmp/counterreset_404.json

say "演示完成，数据目录: $DATA_DIR（服务即将被脚本退出时清理）"
echo "$DATA_DIR" > /tmp/counterreset_demo_dir.txt
