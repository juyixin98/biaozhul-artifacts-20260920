#!/usr/bin/env bash
# 验收脚本：真实 HTTP 走查全部受控场景。
# 前提：metrics-stub(:18081) 与 rollout-api(:18082) 已启动、迁移已执行。
# 用法: scripts/demo.sh
set -euo pipefail

API="${API_URL:-http://localhost:18082}"
STUB="${STUB_URL:-http://localhost:18081}"
TAG="$$_$(date +%s)"

line() { printf '\n========================================\n%s\n========================================\n' "$*"; }
j()    { jq -c "$@"; }

api_post() { # path body
  curl -sS -w '\n%{http_code}' -X POST -H 'Content-Type: application/json' -d "$2" "$API$1"
}
stub_set() { # service scenario bucket correction
  curl -sS -X POST -H 'Content-Type: application/json' "$STUB/scenario" \
    -d "{\"service\":\"$1\",\"name\":\"$2\",\"bucket_seconds\":$3,\"correction_delay_seconds\":$4}" >/dev/null
}
rollout_id()   { echo "$1" | head -1 | jq -r '.id'; }
http_status()  { echo "$1" | tail -1; }
http_body()    { echo "$1" | sed '$d'; }

create() { # name service min window
  api_post /rollouts "{\"name\":\"$1\",\"service\":\"$2\",\"min_samples\":$3,
    \"observation_window_seconds\":$4,\"max_error_rate\":0.05,\"max_p95_latency_ms\":500,
    \"note\":\"v1 frozen at start\"}"
}
evaluate() { api_post "/rollouts/$1/evaluate" '{}'; }
command() { # id type key gen
  api_post "/rollouts/$1/commands" \
    "{\"type\":\"$2\",\"idempotency_key\":\"$3\",\"expected_generation\":$4}"
}

# ---------- 0. 健康检查 ----------
line "0. 健康检查 / 场景列表"
curl -sS "$STUB/scenarios" | j .
curl -sS -o /dev/null -w 'healthz -> %{http_code}\n' "$API/healthz"

# ---------- 1. 健康发布：5% -> 20% -> 50% -> 100% -> completed ----------
line "1. 健康服务走完四个阶段（短暂尖峰不阻断在此用 healthy 场景演示完整推进）"
svc="demo-healthy-$TAG"
stub_set "$svc" healthy 1 0
res=$(create "demo-healthy" "$svc" 100 2); id=$(rollout_id "$res")
echo "created rollout id=$id"
gen=0
for stage in 0 1 2 3; do
  sleep 2.5
  ev=$(evaluate "$id"); st=$(http_status "$ev")
  verdict=$(http_body "$ev" | jq -r '.decision.verdict')
  er=$(http_body "$ev" | jq -r '.decision.error_rate')
  cov=$(http_body "$ev" | jq -r '.decision.coverage')
  echo "stage $stage evaluate: HTTP $st verdict=$verdict error_rate=$er coverage=$cov"
  [ "$verdict" = promote ] || { echo "FAIL: expected promote"; exit 1; }
  cr=$(command "$id" promote "healthy-p-$stage-$TAG" "$gen"); cs=$(http_status "$cr")
  result=$(http_body "$cr" | jq -r '.result')
  gen=$((gen+1))
  echo "stage $stage promote : HTTP $cs result=$result new_generation=$gen"
  [ "$cs" = 200 ] && [ "$result" = applied ] || { echo FAIL; exit 1; }
done
curl -sS "$API/rollouts/$id" | j '{id,status,stage_idx,stage_weight,generation}'

# ---------- 2. 短暂尖峰：单桶越界，聚合健康 → promote ----------
line "2. 短暂尖峰（第 2 桶 8% 错误 / 900ms，聚合 2% / 315ms）"
svc="demo-spike-$TAG"
stub_set "$svc" transient_spike 1 0
id=$(rollout_id "$(create "demo-spike" "$svc" 100 4)")
sleep 4.5
ev=$(evaluate "$id")
http_body "$ev" | j '{verdict:.decision.verdict, reason:.decision.reason,
  samples:.decision.samples, error_rate:.decision.error_rate,
  p95_latency_ms:.decision.p95_latency_ms,
  spike_bucket:(.decision.buckets[] | select(.errors==8))}'
cr=$(command "$id" promote "spike-p-$TAG" 0)
http_body "$cr" | j '{result, stage_idx:.rollout.stage_idx, weight:.rollout.stage_weight}'
[ "$(http_status "$cr")" = 200 ] || { echo FAIL; exit 1; }

# ---------- 3. 持续退化：violation → 推进被拒 → 暂停 → 回退 ----------
line "3. 持续退化（6% / 800ms）→ violation、暂停、回退"
svc="demo-deg-$TAG"
stub_set "$svc" sustained_degradation 1 0
id=$(rollout_id "$(create "demo-degraded" "$svc" 100 2)")
sleep 2.5
ev=$(evaluate "$id")
http_body "$ev" | j '{verdict:.decision.verdict, reason:.decision.reason,
  error_rate:.decision.error_rate, p95_latency_ms:.decision.p95_latency_ms,
  coverage:.decision.coverage}'
cr=$(command "$id" promote "deg-p-$TAG" 0)
echo "promote  -> HTTP $(http_status "$cr") result=$(http_body "$cr" | jq -r '.result')"
[ "$(http_status "$cr")" = 409 ] || { echo FAIL; exit 1; }
cr=$(command "$id" pause "deg-pause-$TAG" 0)
echo "pause    -> HTTP $(http_status "$cr") result=$(http_body "$cr" | jq -r '.result') gen=$(http_body "$cr" | jq -r '.rollout.generation')"
cr=$(command "$id" rollback "deg-rb-$TAG" 1)
echo "rollback -> HTTP $(http_status "$cr") result=$(http_body "$cr" | jq -r '.result') status=$(http_body "$cr" | jq -r '.rollout.status')"
cr=$(command "$id" rollback "deg-rb2-$TAG" 2)
echo "二次回退 -> HTTP $(http_status "$cr") result=$(http_body "$cr" | jq -r '.result')（状态拒绝）"

# ---------- 4. 样本不足：hold ----------
line "4. 样本不足（5 请求/桶，min=100）→ hold，推进拒绝"
svc="demo-thin-$TAG"
stub_set "$svc" insufficient_samples 1 0
id=$(rollout_id "$(create "demo-thin" "$svc" 100 2)")
sleep 2.5
ev=$(evaluate "$id")
http_body "$ev" | j '{verdict:.decision.verdict, reason:.decision.reason, samples:.decision.samples}'
cr=$(command "$id" promote "thin-p-$TAG" 0)
echo "promote  -> HTTP $(http_status "$cr") result=$(http_body "$cr" | jq -r '.result')"
[ "$(http_status "$cr")" = 409 ] || { echo FAIL; exit 1; }

# ---------- 5. 指标中断：unknown，绝不判健康 ----------
line "5. 指标中断 → unknown（非健康），证据里无任何指标值"
svc="demo-outage-$TAG"
stub_set "$svc" metrics_outage 1 0
id=$(rollout_id "$(create "demo-outage" "$svc" 100 2)")
sleep 2.5
ev=$(evaluate "$id")
http_body "$ev" | j '{verdict:.decision.verdict, reason:.decision.reason,
  metrics_status:.decision.metrics_status, error_rate:.decision.error_rate,
  p95_latency_ms:.decision.p95_latency_ms, coverage:.decision.coverage}'
verdict=$(http_body "$ev" | jq -r '.decision.verdict'); [ "$verdict" = unknown ] || { echo FAIL; exit 1; }
cr=$(command "$id" promote "out-p-$TAG" 0)
echo "promote  -> HTTP $(http_status "$cr") result=$(http_body "$cr" | jq -r '.result')（未知不允许推进）"

# ---------- 6. 回退后迟到成功 ----------
line "6. 回退后迟到成功：首版 10% 错误 → 回退；订正到达 → 同窗口转为健康"
svc="demo-late-$TAG"
stub_set "$svc" late_success 1 8
id=$(rollout_id "$(create "demo-late" "$svc" 100 2)")
sleep 2.5
ev=$(evaluate "$id")
echo "首版判定: $(http_body "$ev" | jq -c '{verdict:.decision.verdict,error_rate:.decision.error_rate}')"
cr=$(command "$id" rollback "late-rb-$TAG" 0)
echo "回退    : HTTP $(http_status "$cr") status=$(http_body "$cr" | jq -r '.rollout.status')"
echo "等待迟到订正数据（场景开始后 8s）..."
sleep 6
ev=$(evaluate "$id")
echo "订正判定: $(http_body "$ev" | jq -c '{verdict:.decision.verdict,error_rate:.decision.error_rate,p95_latency_ms:.decision.p95_latency_ms}')"
http_body "$ev" | jq '.decision.buckets[0] | {samples,errors,p95_latency_ms,ingested_at}'
[ "$(http_body "$ev" | jq -r '.decision.verdict')" = promote ] || { echo FAIL; exit 1; }

# ---------- 7. 幂等与代次 ----------
line "7. 幂等键重试不重复执行；过期代次拒绝；同键不同体拒绝"
svc="demo-idem-$TAG"
stub_set "$svc" healthy 1 0
id=$(rollout_id "$(create "demo-idem" "$svc" 100 1)")
sleep 1.5
key="idem-p-$TAG"
cr=$(command "$id" promote "$key" 0)
echo "首次   -> HTTP $(http_status "$cr") stage=$(http_body "$cr" | jq -r '.rollout.stage_idx') gen=$(http_body "$cr" | jq -r '.rollout.generation')"
# 重试：单独取一次响应头确认 Idempotent-Replay
replay_hdr=$(curl -sS -D - -o /tmp/replay-body.json -X POST -H 'Content-Type: application/json' \
  -d "{\"type\":\"promote\",\"idempotency_key\":\"$key\",\"expected_generation\":0}" \
  "$API/rollouts/$id/commands" | tr -d '\r' | awk '/^[Ii]dempotent-[Rr]eplay:/{print $2}')
echo "重试   -> Idempotent-Replay=$replay_hdr $(jq -c '{result, stage:.rollout.stage_idx, gen:.rollout.generation}' /tmp/replay-body.json)"
[ "$replay_hdr" = true ] || { echo "FAIL: 重试未命中幂等重放"; exit 1; }
stage=$(jq -r '.rollout.stage_idx' /tmp/replay-body.json); [ "$stage" = 1 ] || { echo "FAIL: 重试重复推进"; exit 1; }
cr=$(command "$id" rollback "idem-rb-stale-$TAG" 0)
echo "过期代次(0) -> HTTP $(http_status "$cr") result=$(http_body "$cr" | jq -r '.result')"
[ "$(http_status "$cr")" = 409 ] || { echo FAIL; exit 1; }
code=$(curl -sS -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' \
  -d "{\"type\":\"rollback\",\"idempotency_key\":\"$key\",\"expected_generation\":1}" "$API/rollouts/$id/commands")
echo "同键不同体(SHA-256 不一致) -> HTTP $code"
[ "$code" = 409 ] || { echo FAIL; exit 1; }

line "全部验收场景通过 ✅"
echo "决策证据示例: $API/rollouts/<id>/decisions"
