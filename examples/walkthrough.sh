#!/usr/bin/env bash
# 端到端走查：构建 → 启动服务（本地持久化）→ curl 摄入/推进水位/查询，
# 用 jq 对全部验收点做断言；最后重启进程验证 WAL 重放。
#
# 不依赖壁钟断言任何因果关系：所有 receive_ns / watermark_ns 均为合成逻辑时间。
# 仅在“等待端口起来”时轮询 healthz。
set -euo pipefail

cd "$(dirname "$0")/.."

# 随机挑选空闲端口，避免与机器上其他服务冲突。
PORT="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')"
ADDR="127.0.0.1:${PORT}"
BASE="http://${ADDR}"
DATA_DIR="$(pwd)/data-e2e"
BIN="$(pwd)/bin/trace-server"

FAIL=0
SRV_PID=""
pass() { printf '  \033[32mPASS\033[0m %s\n' "$1"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$1"; FAIL=1; }

trap '[[ -n "$SRV_PID" ]] && kill "$SRV_PID" 2>/dev/null || true' EXIT

start_server() {
  "$BIN" -addr "$ADDR" -data-dir "$DATA_DIR" -timeout-ns 100 -skew-tolerance-ns 1000 \
    >>data-e2e-server.log 2>&1 &
  SRV_PID=$!
  for _ in $(seq 1 50); do
    if ! kill -0 "$SRV_PID" 2>/dev/null; then
      echo "server exited early; log:"; cat data-e2e-server.log; exit 2
    fi
    curl -sf "$BASE/healthz" >/dev/null && return 0
    sleep 0.1
  done
  echo "server did not become ready"; exit 2
}

echo "== 构建 =="
go build -o "$BIN" ./cmd/trace-server
rm -rf "$DATA_DIR" data-e2e-server.log

echo "== 启动服务（持久化目录 $DATA_DIR，端口 $PORT）=="
start_server

echo "== 场景 1：缺根 + 子先父后，超时不完整 =="
curl -sS -X POST "$BASE/v1/spans" -H 'Content-Type: application/json' \
  --data @examples/01-ingest-missing-root.json -o resp-01.json
curl -sS -X POST "$BASE/v1/watermark" -H 'Content-Type: application/json' \
  --data @examples/02-watermark-timeout.json -o resp-02.json

jq -e '.emitted_revisions | length == 1' resp-02.json >/dev/null \
  && pass "水位 200 产生 1 个修订" || fail "未产生单个修订"
jq -e '.emitted_revisions[0].complete == false' resp-02.json >/dev/null \
  && pass "rev1 complete=false（不完整标志）" || fail "rev1 误报完整"
jq -e '.emitted_revisions[0].reasons | index("timeout")' resp-02.json >/dev/null \
  && pass "rev1 reasons 含 timeout" || fail "缺 timeout 原因"
jq -e '.emitted_revisions[0].reasons | index("missing-root") and index("missing-parent")' resp-02.json >/dev/null \
  && pass "rev1 标记缺根/缺父" || fail "未标记缺根缺父"
jq -e '.emitted_revisions[0].span_ids | sort == ["c1","g1"]' resp-02.json >/dev/null \
  && pass "rev1 拼装出乱序到达的两个 span（不含 root）" || fail "rev1 span 集合错误"
jq -e '.emitted_revisions[0].missing_parents == [{"child_span_id":"c1","missing_parent_span_id":"root"}]' resp-02.json >/dev/null \
  && pass "悬空父边精确记录" || fail "悬空父边记录错误"

echo "== 场景 2：根迟到，生成新修订并保持逐版包含 =="
curl -sS -X POST "$BASE/v1/spans" -H 'Content-Type: application/json' \
  --data @examples/03-ingest-late-root.json -o resp-03.json
curl -sS -X POST "$BASE/v1/watermark" -H 'Content-Type: application/json' \
  --data @examples/04-watermark-revise.json -o resp-04.json

jq -e '.emitted_revisions | length == 1' resp-04.json >/dev/null \
  && pass "迟到根触发新修订" || fail "未触发新修订"
jq -e '.emitted_revisions[0].revision == 2 and .emitted_revisions[0].revised_of == 1' resp-04.json >/dev/null \
  && pass "rev2 编号=2 且 revised_of=1" || fail "修订链错误"
jq -e '.emitted_revisions[0].complete == true' resp-04.json >/dev/null \
  && pass "rev2 complete=true" || fail "rev2 未完整"
jq -e '.emitted_revisions[0].reasons | index("late-arrival-revision")' resp-04.json >/dev/null \
  && pass "rev2 reasons 含 late-arrival-revision" || fail "缺迟到修订原因"
jq -e '.emitted_revisions[0].root_span_ids == ["root"]' resp-04.json >/dev/null \
  && pass "根唯一且为 root（只按父子图判定）" || fail "根识别错误"
jq -e '(.emitted_revisions[0].span_ids | sort) == (["c1","g1","root"] | sort)' resp-04.json >/dev/null \
  && pass "包含关系：rev2 ⊇ rev1 且多出 root" || fail "逐版包含被破坏"
# 历史版本仍可取、且冻结在 2 个 span。
curl -sS "$BASE/v1/traces/tr-demo/revisions/1" -o resp-rev1.json
jq -e '(.span_ids | length) == 2 and .complete == false' resp-rev1.json >/dev/null \
  && pass "历史 rev1 仍可取且内容冻结" || fail "rev1 历史快照错误"

echo "== 场景 3：精确重复忽略 / 负载冲突先到先得 =="
curl -sS -X POST "$BASE/v1/spans" -H 'Content-Type: application/json' \
  --data @examples/05-ingest-duplicates.json -o resp-05.json
jq -e '.results[0].accepted == true and .results[0].duplicate == false' resp-05.json >/dev/null \
  && pass "首份 span 被接受" || fail "首份未接受"
jq -e '.results[1].accepted == false and .results[1].duplicate == true and .results[1].conflict == null' resp-05.json >/dev/null \
  && pass "精确重复识别且静默忽略（receive_ns 差异不算冲突）" || fail "精确重复处理错误"
jq -e '.results[2].conflict.kind == "payload-conflict"
  and (.results[2].conflict.fields | index("operation"))
  and (.results[2].conflict.fields | index("duration_nanos"))' resp-05.json >/dev/null \
  && pass "负载冲突识别并列出冲突字段" || fail "冲突识别错误"

echo "== 场景 4：跨服务时钟偏差（信息性，不动结构）=="
curl -sS -X POST "$BASE/v1/spans" -H 'Content-Type: application/json' \
  --data @examples/06-ingest-clock-skew.json -o resp-06.json
jq -e '.emitted_revisions[0].complete == true' resp-06.json >/dev/null \
  && pass "偏差不影响完整性" || fail "偏差误判为不完整"
jq -e '.emitted_revisions[0].clock_skew | map(.kind) | index("starts-before-parent") and index("ends-after-parent")' resp-06.json >/dev/null \
  && pass "检测到子早于父开始、晚于父结束两类偏差" || fail "时钟偏差漏报"
jq -e '.emitted_revisions[0].root_span_ids == ["root"]' resp-06.json >/dev/null \
  && pass "因果仍由父子图决定，root 为根" || fail "偏差污染了结构判定"

echo "== 场景 5：父链循环 =="
curl -sS -X POST "$BASE/v1/spans" -H 'Content-Type: application/json' \
  --data @examples/07-ingest-cycle.json -o resp-07.json
jq -e '.emitted_revisions[0].cycles == [["a","b"]]' resp-07.json >/dev/null \
  && pass "检测到环 a↛b↛a（归一化输出）" || fail "循环检测错误"
jq -e '.emitted_revisions[0].complete == false
  and (.emitted_revisions[0].reasons | index("cycle-detected"))' resp-07.json >/dev/null \
  && pass "循环 trace 不完整且标记 cycle-detected" || fail "循环处理错误"

echo "== 场景 6：重启进程，WAL 重放恢复全部修订 =="
kill "$SRV_PID"; SRV_PID=""
start_server
curl -sS "$BASE/v1/traces" -o resp-list.json
jq -e '.watermark_ns == 500' resp-list.json >/dev/null \
  && pass "水位恢复到 500" || fail "水位恢复错误"
jq -e '[.traces[].trace_id] | sort == ["tr-cycle","tr-demo","tr-dup","tr-skew"]' resp-list.json >/dev/null \
  && pass "四个 trace 全部恢复" || fail "trace 集合恢复错误"
curl -sS "$BASE/v1/traces/tr-demo/revisions/2" -o resp-rev2.json
jq -e '.revision == 2 and .complete == true and (.span_ids | length == 3)' resp-rev2.json >/dev/null \
  && pass "tr-demo rev2 经重放可逐版查询" || fail "重放后修订不一致"

echo
if [[ "$FAIL" -eq 0 ]]; then
  echo "E2E: ALL ASSERTIONS PASSED"
else
  echo "E2E: SOME ASSERTIONS FAILED（见上方 FAIL）"
fi
exit "$FAIL"
