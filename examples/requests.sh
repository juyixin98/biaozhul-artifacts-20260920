#!/usr/bin/env bash
# 请求样例：覆盖提交/准入拒绝/排队取消/运行超时/统计与事件查询。
# 用法: ./examples/requests.sh [host:port]   (默认 127.0.0.1:8080)
set -euo pipefail
B="http://${1:-127.0.0.1:8080}"

echo '== 1. 快速完成的作业 =='
curl -s -X POST "$B/jobs" -d '{"id":"quick","exec_bound_ms":200,"deadline_in_ms":30000,"simulate_actual_ms":50}'; echo
sleep 0.4
curl -s "$B/jobs/quick"; echo

echo '== 2. 长作业占住机器后，预计不可满足的作业被准入拒绝 (422) =='
curl -s -X POST "$B/jobs" -d '{"id":"long","exec_bound_ms":2000,"deadline_in_ms":60000,"simulate_actual_ms":1800}'; echo
curl -s -X POST "$B/jobs" -d '{"id":"hopeless","exec_bound_ms":1500,"deadline_in_ms":500}'; echo

echo '== 3. 排队中的作业可以取消；重复取消返回 409，不会重复释放资源 =='
curl -s -X POST "$B/jobs" -d '{"id":"queued","exec_bound_ms":100,"deadline_in_ms":60000}'; echo
curl -s -X POST "$B/jobs/queued/cancel"; echo
curl -s -o /dev/null -w 'duplicate cancel -> HTTP %{http_code}\n' -X POST "$B/jobs/queued/cancel"
sleep 2.2

echo '== 4. 实际执行超出声明上界的作业 -> TIMEOUT（区别于准入拒绝） =='
curl -s -X POST "$B/jobs" -d '{"id":"overrun","exec_bound_ms":300,"deadline_in_ms":60000,"simulate_actual_ms":5000}'; echo
sleep 0.8
curl -s "$B/jobs/overrun"; echo

echo '== 5. 截止时间统计与资源守恒（slot_acquired == slot_released，slot_in_use == false） =='
curl -s "$B/stats"; echo

echo '== 6. 结构化事件流 =='
curl -s "$B/events"; echo
