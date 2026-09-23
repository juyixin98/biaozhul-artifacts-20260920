#!/usr/bin/env bash
# End-to-end demo against a running tokenbudget server using examples/config.json
# (all buckets stopped with fixed bursts, so results are deterministic and do
# not depend on wall-clock refill).
# Usage: scripts/e2e_demo.sh [BASE_URL]
set -u
B="${1:-http://127.0.0.1:18092}"
SEC=1000000000

hr(){ printf '\n================ %s ================\n' "$1"; }
code(){
  local method="$1" path="$2" body="${3:-}"
  if [ -n "$body" ]; then
    curl -sS -X "$method" "$B$path" -H 'Content-Type: application/json' \
      -d "$body" -w '\n[HTTP %{http_code}]\n'
  else
    curl -sS -X "$method" "$B$path" -w '\n[HTTP %{http_code}]\n'
  fi
}

hr "health"
code GET /healthz

hr "INITIAL STATE"
echo "global: stopped, 100 burst | acme: stopped, 2 burst | beta: stopped, 3 burst"
code GET /state

hr "LAYER-ATOMICITY via two tenants (global=3 burst, set below)"
code PUT /config/global "{\"rate\":{\"rate_num\":0,\"rate_den_ns\":0},\"capacity\":3,\"initial_tokens\":3}"

hr "REQUEST acme x1 -> 200 (global 3->2, acme 2->1)"
code POST /request '{"tenant":"acme","tokens":1}'

hr "REQUEST beta x1 -> 200 (global 2->1, beta 3->2)"
code POST /request '{"tenant":"beta","tokens":1}'

hr "REQUEST acme x1 -> 200 (global 1->0, acme 1->0). Now global is EMPTY."
code POST /request '{"tenant":"acme","tokens":1}'

hr "REQUEST beta x1 -> 429 GLOBAL layer blocks. beta still has 2 -> MUST stay 2 (no partial)."
code POST /request '{"tenant":"beta","tokens":1}'

hr "REQUEST acme x1 -> 429 (both layers empty). Nothing changes."
code POST /request '{"tenant":"acme","tokens":1}'

hr "STATE: expect global=0, acme=0, beta=2"
code GET /state

hr "OVERSIZED x99 -> 422, and INVALID inputs -> 400"
code POST /request '{"tenant":"acme","tokens":99}'
code POST /request '{"tokens":1}'
code POST /request '{not-json'

hr "CONFIG SWITCH: grow global 3->50 grants NOTHING (still 0 available)"
code PUT /config/global '{"rate":{"rate_num":0,"rate_den_ns":0},"capacity":50}'

hr "CONFIG SWITCH: shrink beta 3->1 clamps its stock 2->1 (never invents tokens)"
code PUT /config/tenants/beta '{"rate":{"rate_num":0,"rate_den_ns":0},"capacity":1}'

hr "CONFIG SWITCH: invalid (capacity 0) -> 400"
code PUT /config/global '{"rate":{"rate_num":0,"rate_den_ns":0},"capacity":0}'

hr "RESUME: set global to 10/s and acme to 1 token/s. Stock was 0; no backfill for stopped time."
code PUT /config/global "{\"rate\":{\"rate_num\":10,\"rate_den_ns\":$SEC},\"capacity\":50}"
code PUT /config/tenants/acme "{\"rate\":{\"rate_num\":1,\"rate_den_ns\":$SEC},\"capacity\":4}"

hr "REQUEST acme immediately -> still 429 (config change mints nothing instantly)"
code POST /request '{"tenant":"acme","tokens":1}'

hr "SCHEDULE acme x1 -> 202, ready in ~1s (tenant 1/s is the bottleneck; firm reservation)"
code POST /schedule '{"tenant":"acme","name":"job-A","tokens":1}'

hr "SCHEDULE acme x1 -> 202, queues behind, ready in ~2s"
code POST /schedule '{"tenant":"acme","name":"job-B","tokens":1}'

hr "SCHEDULE acme x99 -> 422 (over capacity 4, rejected, no consumption)"
code POST /schedule '{"tenant":"acme","name":"too-big","tokens":99}'

hr "jobs immediately (2 queued, 1 rejected)"
code GET /jobs

echo; echo "sleep 3s for real-time refill + deferred execution..."
sleep 3

hr "jobs after 3s -> job-A / job-B succeeded"
code GET /jobs

hr "FINAL STATE (acme refilled to 4 cap; 2 reservations consumed)"
code GET /state

hr "STRUCTURED EVENT LOG"
curl -sS "$B/events" | python3 -c '
import sys,json
d=json.load(sys.stdin)
ev=d["events"]
print("total events:",len(ev))
for e in ev: print(json.dumps(e,separators=(",",":")))
'
