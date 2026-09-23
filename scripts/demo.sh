#!/usr/bin/env bash
# 端到端验收演示：真实启动三个进程（协调器 + 目标桩 + 中继），覆盖题目全部场景。
#
# 用法： ./scripts/demo.sh
# 依赖： cargo（自动编译）、curl、jq、本地 PostgreSQL（admin/relay_coord_dev）
set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

COORD_URL="http://127.0.0.1:8088"
STUB_URL="http://127.0.0.1:9098"
DB_DEMO="postgres://admin:relay_coord_dev@localhost/relay_coord_demo"

COORD_PID=""; STUB_PID=""; WORKER_PIDS=()

cleanup() {
  for p in "${WORKER_PIDS[@]}"; do kill "$p" 2>/dev/null; done
  [ -n "$COORD_PID" ] && kill "$COORD_PID" 2>/dev/null
  [ -n "$STUB_PID" ] && kill "$STUB_PID" 2>/dev/null
}
trap cleanup EXIT

hr() { printf '\n================ %s ================\n' "$*"; }
say() { printf '\n--- %s ---\n' "$*"; }

hr "构建"
cargo build --quiet

say "准备演示数据库 relay_coord_demo"
PGPASSWORD=relay_coord_dev psql -h 127.0.0.1 -U admin -d postgres \
  -c "CREATE DATABASE relay_coord_demo;" 2>/dev/null || true
PGPASSWORD=relay_coord_dev psql "$DB_DEMO" -c \
  "TRUNCATE TABLE messages, channels, fencing_seq RESTART IDENTITY CASCADE;
   INSERT INTO fencing_seq (id, value) VALUES (1,0) ON CONFLICT (id) DO NOTHING;" >/dev/null

hr "启动进程：协调器 :8088，目标桩 :9098"
DATABASE_URL="$DB_DEMO" ./target/debug/coordinator --listen 127.0.0.1:8088 &
COORD_PID=$!
./target/debug/target-stub --listen 127.0.0.1:9098 &
STUB_PID=$!

wait_http() {
  for _ in $(seq 1 100); do
    curl -fsS "$1/healthz" >/dev/null 2>&1 && return 0
    sleep 0.1
  done
  echo "服务未就绪: $1"; exit 1
}
wait_http "$COORD_URL"; wait_http "$STUB_URL"
echo "协调器与目标桩均已就绪"

# 两个中继进程并发工作（跨进程竞争始终存在）。
./target/debug/worker \
  --coordinator "$COORD_URL" --target "$STUB_URL" \
  --relay-id relay-A --lease-ttl 10 --submit-timeout 2 --poll-ms 150 &
WORKER_PIDS+=($!)
./target/debug/worker \
  --coordinator "$COORD_URL" --target "$STUB_URL" \
  --relay-id relay-B --lease-ttl 10 --submit-timeout 2 --poll-ms 150 &
WORKER_PIDS+=($!)

enqueue() { # $1=channel  $2=json-file
  curl -fsS -X POST "$COORD_URL/v1/messages" \
    -H 'content-type: application/json' \
    -d "$(jq -c --arg c "$1" '.channel_id=$c' "$2")"
}
get_msg() { curl -fsS "$COORD_URL/v1/messages/$1"; }
wait_for() { # $1=id $2=status
  for _ in $(seq 1 200); do
    s=$(get_msg "$1" | jq -r .status)
    [ "$s" = "$2" ] && return 0
    sleep 0.25
  done
  echo "超时: $1 未进入 $2"; get_msg "$1"; return 1
}

hr "场景 1：提交成功但响应丢失 —— 先查询再重发，只用同一个稳定 ID"
id=$(enqueue demo-lost examples/enqueue_payment.json | jq -r .submission_id)
echo "消息 ID: $id（幂等键，绝不重新生成）"
# 首次提交：已真实落盘，但响应拖 6 秒（worker 提交超时 2 秒）。
curl -fsS -X PUT "$STUB_URL/admin/rules/$id" -H 'content-type: application/json' \
  -d '{"drop_responses":1,"sleep_secs":6,"commit_delay_secs":0,"query_delay_secs":0,"always_fail":false}' >/dev/null
wait_for "$id" confirmed
echo "最终状态："
get_msg "$id" | jq '{status, nonce, fencing_token, target_tx_id, attempts}'
echo "目标桩记录数（必须恰好 1 条）："
curl -fsS "$STUB_URL/records" | jq --arg id "$id" '[.[]|select(.submission_id==$id)]|length'
echo "目标桩观测到的代际（fencing token 序列）："
curl -fsS "$STUB_URL/result/$id" | jq '{status, tx_id, observed: [.observed[]|{relay_id, fencing_token, replayed}]}'

hr "场景 2：每通道 nonce 顺序 —— 同通道严格有序，跨通道并行"
c1=$(enqueue seq-A examples/enqueue_payment.json | jq -r .submission_id)
c2=$(enqueue seq-A examples/enqueue_payment_2.json | jq -r .submission_id)
o1=$(enqueue seq-B examples/enqueue_oracle.json | jq -r .submission_id)
wait_for "$c1" confirmed; wait_for "$c2" confirmed; wait_for "$o1" confirmed
curl -fsS "$COORD_URL/v1/channels" | jq -r '.[]|select(.channel_id|startswith("seq"))|
  "通道 \(.channel_id): next_nonce=\(.next_nonce) blocked=\(.blocked)"'
echo "链上确认顺序（seq-A 的 nonce 0 必须早于 nonce 1）："
curl -fsS "$STUB_URL/records" | jq -r '.[]|select(.channel_id|startswith("seq"))|
  "\(.channel_id) nonce=\(.nonce) confirmed_at=\(.confirmed_at)"' | sort

hr "场景 3：跨进程竞争 —— 两个真实 worker 进程，每条消息恰好上链一次"
for i in $(seq 1 6); do
  enqueue race examples/enqueue_payment.json | jq -r .submission_id
done > /tmp/race_ids.txt
while read -r r; do wait_for "$r" confirmed; done < /tmp/race_ids.txt
echo "race 通道 6 条消息的 nonce 与执行中继（无重复提交、无空洞）："
curl -fsS "$STUB_URL/records" | jq -r '.[]|select(.channel_id=="race")|
  "nonce=\(.nonce) tx=\(.tx_id[0:18])… 首次中继=\(.observed[0].relay_id) 观测次数=\(.observed|length)"' | sort -V
echo "6 条消息的 tx_id 互不相同："
curl -fsS "$STUB_URL/records" | jq -r '.[]|select(.channel_id=="race")|.tx_id' | sort -u | wc -l

hr "场景 4：失败队头阻塞 —— 阻塞本通道后续，不阻塞其他通道"
x0=$(enqueue blocked examples/enqueue_payment.json | jq -r .submission_id)
x1=$(enqueue blocked examples/enqueue_payment_2.json | jq -r .submission_id)
x2=$(enqueue blocked examples/enqueue_oracle.json | jq -r .submission_id)
y0=$(enqueue free examples/enqueue_oracle.json | jq -r .submission_id)
curl -fsS -X PUT "$STUB_URL/admin/rules/$x1" -H 'content-type: application/json' \
  -d '{"drop_responses":0,"sleep_secs":0,"commit_delay_secs":0,"query_delay_secs":0,"always_fail":true}' >/dev/null
wait_for "$x0" confirmed; wait_for "$x1" failed; wait_for "$y0" confirmed
echo "blocked 通道 nonce=1 失败后，nonce=2 仍被挡住："
get_msg "$x2" | jq '{submission_id:(.submission_id[0:8]), nonce, status}'
echo "其他通道 free 照常确认："
get_msg "$y0" | jq '{nonce, status}'
echo "向 blocked 通道继续投递被明确拒绝（409 channel_blocked）："
curl -sS -o /tmp/blocked_resp -w 'HTTP %{http_code} ' "$COORD_URL/v1/messages" \
  -H 'content-type: application/json' \
  -d '{"channel_id":"blocked","payload":{"x":1}}'; jq -c . /tmp/blocked_resp

hr "场景 5：恢复 —— 排除故障后重试，failed 消息用同一 ID 重新上链，通道解锁"
curl -fsS -X DELETE "$STUB_URL/admin/rules/$x1" >/dev/null
curl -fsS -X POST "$COORD_URL/v1/messages/$x1/retry" | jq '{status, nonce}'
wait_for "$x1" confirmed; wait_for "$x2" confirmed
echo "恢复后 blocked 通道全部确认："
for m in "$x0" "$x1" "$x2"; do get_msg "$m" | jq -c '{nonce, status, tx:(.target_tx_id[0:18])}'; done
curl -fsS "$COORD_URL/v1/channels" | jq -r '.[]|select(.channel_id=="blocked")|"blocked 通道: blocked=\(.blocked)"'
echo "x1 目标桩仍是同一条记录（稳定 ID），状态由 failed 变 confirmed："
curl -fsS "$STUB_URL/result/$x1" | jq '{status, tx_id, observations:(.observed|length)}'

hr "场景 6：fencing —— 旧代中继的迟到回执不能覆盖新代状态"
say "停掉 worker，纯手动控制两代租约（确定性演示）"
for p in "${WORKER_PIDS[@]}"; do kill "$p" 2>/dev/null; done
sleep 0.5

fid=$(enqueue fence examples/enqueue_payment.json | jq -r .submission_id)
# 第一代：手动领 2 秒短租约，然后什么都不做（模拟中继假死）。
old=$(curl -fsS -X POST "$COORD_URL/v1/leases/acquire?relay_id=dead-relay&ttl_secs=2" | jq .fencing_token)
echo "旧代领取 token=$old，等它过期…"
sleep 3
# 第二代：手动领取（消息仍是 leased，未终态）。
new=$(curl -fsS -X POST "$COORD_URL/v1/leases/acquire?relay_id=live-relay&ttl_secs=30" | jq .fencing_token)
echo "新代领取 token=$new（严格递增: $old → $new）"

say "旧代持旧 token 伪造成功回执 → 必须是 stale_token，状态不得被改写"
curl -fsS -X POST "$COORD_URL/v1/leases/$fid/receipt" -H 'content-type: application/json' \
  -d "{\"fencing_token\":$old,\"tx_id\":\"0xFAKE_STALE\",\"result\":{}}" \
  | jq '{accepted, verdict}'
echo "消息仍是 leased、无 tx："
get_msg "$fid" | jq '{status, fencing_token, target_tx_id}'

say "新代真实提交目标链并回执确认"
curl -fsS -X POST "$STUB_URL/submit" -H 'content-type: application/json' \
  -d "{\"submission_id\":\"$fid\",\"channel_id\":\"fence\",\"nonce\":0,
       \"fencing_token\":$new,\"relay_id\":\"live-relay\",\"payload\":{\"k\":\"v\"}}" \
  | jq '{ok, tx_id}'
real_tx=$(curl -fsS "$STUB_URL/result/$fid" | jq -r .tx_id)
curl -fsS -X POST "$COORD_URL/v1/leases/$fid/receipt" -H 'content-type: application/json' \
  -d "{\"fencing_token\":$new,\"tx_id\":\"$real_tx\",\"result\":{\"ok\":true}}" \
  | jq '{accepted, verdict}'

say "终态之后旧代再报失败 → already_final，状态纹丝不动"
curl -fsS -X POST "$COORD_URL/v1/leases/$fid/receipt" -H 'content-type: application/json' \
  -d "{\"fencing_token\":$old,\"error\":\"late failure from dead relay\"}" \
  | jq '{accepted, verdict}'
get_msg "$fid" | jq '{status, fencing_token, target_tx_id}'

hr "场景 7：未知≠失败（手工协议演示，独立 ID 不经协调器）"
uid="11111111-2222-3333-4444-555555555555"
curl -fsS -X PUT "$STUB_URL/admin/rules/$uid" -H 'content-type: application/json' \
  -d '{"drop_responses":1,"sleep_secs":5,"commit_delay_secs":0,"query_delay_secs":0,"always_fail":false}' >/dev/null
say "用 1 秒超时直接提交（必然超时 —— 这只代表结果未知）"
curl -sS --max-time 1 -X POST "$STUB_URL/submit" -H 'content-type: application/json' \
  -d "{\"submission_id\":\"$uid\",\"channel_id\":\"manual\",\"nonce\":0,
       \"fencing_token\":999,\"relay_id\":\"manual-demo\",\"payload\":{\"k\":\"v\"}}" \
  >/dev/null 2>&1
echo "curl 退出码 $?（28=超时）。不报错、不造新 ID，先查询："
curl -fsS "$STUB_URL/result/$uid" | jq '{status, tx_id}'
say "用同一个 $uid 重放提交（幂等，立即返回 replayed=true）"
curl -fsS -X POST "$STUB_URL/submit" -H 'content-type: application/json' \
  -d "{\"submission_id\":\"$uid\",\"channel_id\":\"manual\",\"nonce\":0,
       \"fencing_token\":999,\"relay_id\":\"manual-demo\",\"payload\":{\"k\":\"v\"}}" \
  | jq '{ok, replayed, tx_id}'

hr "全部场景演示完成"
