#!/usr/bin/env bash
# End-to-end demo for the tail-sampling decision backend.
# Scenarios:
#   1. error / slow / fast traces  -> error keep, latency keep, default drop
#   2. late error span AFTER finalization -> decision stays immutable, late recorded
#   3. budget exhaustion -> slow traces explicitly downgraded (degraded=true)
#   4. large trace (1001 spans, 4 errors) -> kept by error policy
#   5. incomplete trace (no root) -> marked FORCED_INCOMPLETE after max TTL
set -euo pipefail

cd "$(dirname "$0")/.."
BIN=/tmp/tailsampled-demo
DATA_DIR=$(mktemp -d /tmp/ts-data-XXXX)
go build -o "$BIN" ./cmd/tailsampled

# Pick a free TCP port (environment may have common demo ports taken).
pick_port() {
  python3 - <<'PY'
import socket
s = socket.socket()
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
s.close()
PY
}
PORT="${PORT:-$(pick_port)}"
BASE="http://127.0.0.1:${PORT}"

# Tight demo config: 1s wait window, 3s TTL, tiny non-refilling budget.
cat > /tmp/ts-demo-config.json <<JSON
{
  "listen": "127.0.0.1:${PORT}",
  "data_dir": "${DATA_DIR}",
  "wait_window": "1s",
  "max_ttl": "3s",
  "snapshot_interval": "2s",
  "error_policy": true,
  "latency_threshold_ms": 500,
  "probabilistic_rate": 0,
  "budget_capacity": 3,
  "budget_refill_per_sec": 0
}
JSON

"$BIN" -config /tmp/ts-demo-config.json &
PID=$!
cleanup() { kill "$PID" 2>/dev/null || true; rm -f /tmp/ts-demo-config.json "$BIN"; }
trap cleanup EXIT

echo "==> waiting for server"
for i in $(seq 1 50); do curl -sf "$BASE/healthz" >/dev/null && break; sleep 0.1; done

post() { curl -s -X POST "$BASE$1" -H 'Content-Type: application/json' -d @"$2" | jq .; }
get()  { curl -s "$BASE$1" | jq .; }

echo; echo "==> scenario 1: ingest error trace, slow trace (budget) and fast trace"
post /v1/spans examples/spans_error.json >/dev/null
# slow trace with demo threshold semantics (500ms): example file is 2500ms -> keep
jq '.spans[0].trace_id="trace-slow-demo" | .spans[1].trace_id="trace-slow-demo"' \
  examples/spans_slow.json > /tmp/slow.json
post /v1/spans /tmp/slow.json >/dev/null
post /v1/spans examples/spans_fast.json >/dev/null

echo "    waiting for 1s decision wait window..."; sleep 1.3
echo "--- error trace decision (expect kept=true, policy=error)"
get /v1/decisions/trace-err-001
echo "--- slow trace decision (expect kept=true, policy=latency, budget used)"
get /v1/decisions/trace-slow-demo
echo "--- fast trace decision (expect kept=false, DEFAULT_DROP)"
get /v1/decisions/trace-fast-001

echo; echo "==> scenario 2: late error span arrives AFTER decision (must stay dropped & be recorded)"
cat > /tmp/late.json <<'JSON'
{"spans":[{"trace_id":"trace-fast-001","span_id":"late-err","parent_span_id":"root",
"name":"late async failure","service":"gateway","status":"ERROR","error_event":"queue nack",
"start_time_ms":1700000002100,"duration_ms":2}]}
JSON
post /v1/spans /tmp/late.json
echo "--- decision is immutable; late_spans lists the late arrival"
get /v1/decisions/trace-fast-001

echo; echo "==> scenario 3: budget exhaustion (capacity=3, 0 refill)"
echo "    one more slow trace consumes the 3rd token -> kept"
cat > /tmp/slow2.json <<'JSON'
{"spans":[{"trace_id":"trace-slow-002","span_id":"root","name":"slow q",
"status":"OK","start_time_ms":1700000003000,"duration_ms":900}]}
JSON
post /v1/spans /tmp/slow2.json >/dev/null
sleep 1.2
get /v1/decisions/trace-slow-002 | jq '{trace_id,kept,policy,degraded,reason_code,budget_used}'
echo "    two more slow traces: expect degraded=true BUDGET_DROP downgrade"
for n in 003 004; do
  cat > /tmp/slow$n.json <<JSON
{"spans":[{"trace_id":"trace-slow-$n","span_id":"root","name":"slow q",
"status":"OK","start_time_ms":1700000004000,"duration_ms":900}]}
JSON
  post /v1/spans /tmp/slow$n.json >/dev/null
done
sleep 1.3
get /v1/decisions/trace-slow-003
get /v1/decisions/trace-slow-004

echo; echo "==> scenario 4: large trace (1001 spans, 4 error spans)"
python3 - <<'PY'
import json
spans=[{"trace_id":"trace-big","span_id":"root","parent_span_id":"",
        "name":"root","status":"OK","start_time_ms":1700000010000,"duration_ms":6000}]
for i in range(1000):
    spans.append({"trace_id":"trace-big","span_id":f"s{i:04d}","parent_span_id":"root",
                  "name":f"work-{i}","status":"ERROR" if i%250==0 else "OK",
                  "start_time_ms":1700000010000+i,"duration_ms":2})
json.dump({"spans":spans}, open("/tmp/big.json","w"))
print("generated", len(spans), "spans")
PY
post /v1/spans /tmp/big.json | jq '{received,accepted,duplicates,late_after_decision}'
sleep 1.3
echo "    budget now exhausted: error trace is kept BEYOND budget but degraded=true"
get /v1/decisions/trace-big | jq '{trace_id,kept,policy,span_count,error_count,duration_ms,degraded,budget_used}'

echo; echo "==> scenario 5: incomplete trace (child span only, root never arrives)"
cat > /tmp/orphan.json <<'JSON'
{"spans":[{"trace_id":"trace-orphan","span_id":"child-only","parent_span_id":"missing-root",
"name":"follower op","status":"OK","start_time_ms":1700000020000,"duration_ms":10}]}
JSON
post /v1/spans /tmp/orphan.json >/dev/null
echo "    immediately querying -> 404 (not finalized yet)"
get /v1/decisions/trace-orphan
echo "    waiting past max_ttl=3s -> FORCED_INCOMPLETE marker"; sleep 3.3
get /v1/decisions/trace-orphan

echo; echo "==> final stats"
get /v1/stats
echo; echo "==> decision list (all, newest first)"
get "/v1/decisions?limit=20" | jq '.decisions[] | {trace_id,kept,complete,policy,reason_code,degraded}'

echo; echo "DEMO DONE"
