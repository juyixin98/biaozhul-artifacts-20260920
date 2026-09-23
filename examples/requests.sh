#!/usr/bin/env bash
# 请求样例：对本地运行的 tokenbudgetd 演示分层令牌预算的全部关键行为。
#
# 用法（在仓库根目录）：
#   go build -o bin/tokenbudgetd ./cmd/tokenbudgetd
#   ./bin/tokenbudgetd -virtual -addr 127.0.0.1:8099 -config examples/config.json
#   # 另开一个终端：
#   bash examples/requests.sh
#
# 使用 -virtual 后时间只在调用 /internal/clock/advance 时前进，因此补充行为
# 完全确定、可复现；去掉 -virtual 即用真实单调时钟。
set -u
B="${BASE_URL:-http://127.0.0.1:8099}"

say() { printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }
code() { curl -s -o /tmp/tb_body.json -w '%{http_code}' "$@"; }

say "健康检查"
curl -s "$B/healthz"; echo

say "读取当前配置（global / default / 租户覆盖）"
curl -s "$B/v1/config" | python3 -m json.tool

say "突发：租户 a 连续 5 个 cost=1（默认层 burst=5）全部成功"
for i in 1 2 3 4 5; do
  c=$(code -X POST "$B/v1/acquire" -H 'Content-Type: application/json' \
        -d '{"tenant":"a","cost":"1"}')
  echo "request $i -> HTTP $c"
done

say "第 6 个被租户层拒绝（429），retry_after 给出精确等待提示"
curl -s -X POST "$B/v1/acquire" -H 'Content-Type: application/json' \
  -d '{"tenant":"a","cost":"1"}' | python3 -m json.tool

say "原子性：用租户 b 耗尽全局层剩余 5 个"
for i in 1 2 3 4 5; do
  c=$(code -X POST "$B/v1/acquire" -H 'Content-Type: application/json' \
        -d '{"tenant":"b","cost":"1"}')
  echo "b request $i -> HTTP $c"
done

say "全新租户 c 自身桶是满的(5)，但全局=0：拒绝全局且其桶保持 5（无部分消费）"
curl -s -X POST "$B/v1/acquire" -H 'Content-Type: application/json' \
  -d '{"tenant":"c","cost":"1"}' | python3 -m json.tool
echo "-- 桶状态：global 必须为 0，c 的 tenant 必须仍为 5 --"
curl -s "$B/v1/buckets/c" | python3 -m json.tool

say "推进虚拟时钟 500ms：全局 +5(10/s)，租户 c +1(2/s)"
curl -s -X POST "$B/internal/clock/advance" -H 'Content-Type: application/json' -d '{"ms":500}'
echo
curl -s "$B/v1/buckets/c" | python3 -m json.tool

say "精确分数速率：vip 配置为 1/3 token/秒，burst 3"
for i in 1 2 3; do
  c=$(code -X POST "$B/v1/acquire" -H 'Content-Type: application/json' \
        -d '{"tenant":"vip","cost":"1"}')
  echo "vip burst $i -> HTTP $c"
done
echo "推进 1 秒：只积 0.333333 个，不足以取整 -> 仍 429"
curl -s -X POST "$B/internal/clock/advance" -H 'Content-Type: application/json' -d '{"ms":1000}' >/dev/null
curl -s -X POST "$B/v1/acquire" -H 'Content-Type: application/json' \
  -d '{"tenant":"vip","cost":"1"}' | python3 -m json.tool
echo "再推进 2 秒（累计 3 秒）：恰好 1 个 -> 成功；紧接着第二个失败（无漂移）"
curl -s -X POST "$B/internal/clock/advance" -H 'Content-Type: application/json' -d '{"ms":2000}' >/dev/null
c=$(code -X POST "$B/v1/acquire" -H 'Content-Type: application/json' -d '{"tenant":"vip","cost":"1"}'); echo "第一个 -> HTTP $c"
c=$(code -X POST "$B/v1/acquire" -H 'Content-Type: application/json' -d '{"tenant":"vip","cost":"1"}'); echo "紧接第二个 -> HTTP $c（应为 429）"

say "动态配置切换：提高速率/突发，存量保留不补满"
curl -s -X PUT "$B/v1/config" -H 'Content-Type: application/json' -d '{
  "global":  {"rate":"100","burst":"20"},
  "default": {"rate":"100","burst":"20"}
}' | python3 -m json.tool
curl -s "$B/v1/buckets/a" | python3 -m json.tool

say "拒绝浮点 JSON 数字（必须以字符串传精确小数）"
curl -s -w '\nHTTP %{http_code}\n' -X POST "$B/v1/acquire" -H 'Content-Type: application/json' \
  -d '{"tenant":"a","cost":0.1}'

say "结构化事件：查看最近 5 条"
curl -s "$B/v1/events?limit=5" | python3 -m json.tool
