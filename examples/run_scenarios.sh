#!/usr/bin/env bash
# Runs the three acceptance scenarios against a running autoscaler server.
# Usage: ./examples/run_scenarios.sh [base-url]   (default http://localhost:8080)
set -euo pipefail

BASE="${1:-http://localhost:8080}"

post() { # path, json
  curl -sS -X POST "$BASE$1" -H 'Content-Type: application/json' -d "$2"
}

# samples_json <time> <pod_start> <value> <pod...>
samples_json() {
  local t="$1" start="$2" v="$3"; shift 3
  local out='{"samples":[' first=1
  for p in "$@"; do
    [ $first -eq 0 ] && out+=','
    out+="{\"time\":\"$t\",\"pod\":\"$p\",\"pod_start\":\"$start\",\"value\":$v}"
    first=0
  done
  echo "$out]}"
}

evaluate() { # label, rfc3339-time
  echo "----- $1 (evaluate at $2) -----"
  post /v1/evaluate "{\"time\":\"$2\"}"
  echo
}

reset() { # replicas
  echo "===== reset to $1 replicas ====="
  post /v1/reset "{\"replicas\":$1}" >/dev/null
}

START="2026-09-24T10:00:00Z" # all pods started long ago -> no cold start

echo
echo "########## Scenario A: spike (rate limit + cooldown + scale-down protection) ##########"
reset 2
post /v1/metrics "$(samples_json 2026-09-24T12:00:00Z $START 250 p1 p2)" >/dev/null
evaluate "A1 spike 5x load, raw=10 but step limit +2" 2026-09-24T12:00:00Z
post /v1/metrics "$(samples_json 2026-09-24T12:00:30Z $START 250 p1 p2 p3 p4)" >/dev/null
evaluate "A2 30s later: scale-up cooldown blocks" 2026-09-24T12:00:30Z
post /v1/metrics "$(samples_json 2026-09-24T12:01:01Z $START 250 p1 p2 p3 p4)" >/dev/null
evaluate "A3 61s later: cooldown over, +2 again" 2026-09-24T12:01:01Z
post /v1/metrics "$(samples_json 2026-09-24T12:02:01Z $START 10 p1 p2 p3 p4 p5 p6)" >/dev/null
evaluate "A4 load collapsed: stabilization window holds" 2026-09-24T12:02:01Z
post /v1/metrics "$(samples_json 2026-09-24T12:07:01Z $START 10 p1 p2 p3 p4 p5 p6)" >/dev/null
evaluate "A5 window passed: scale down, rate-limited to -1" 2026-09-24T12:07:01Z

echo
echo "########## Scenario B: sustained growth (staircase up to max) ##########"
reset 2
t=12:20:00
for i in 0 1 2 3; do
  ts="2026-09-24T12:2${i}:00Z"
  n=$(curl -sS "$BASE/v1/state" | grep -oE '"replicas": *[0-9]+' | grep -oE '[0-9]+')
  pods=$(seq -f 'p%g' 1 "$n" | tr '\n' ' ')
  post /v1/metrics "$(samples_json "$ts" $START 140 $pods)" >/dev/null
  evaluate "B$((i+1)) load 140/pod on $n replicas (ratio 2.8)" "$ts"
done

echo
echo "########## Scenario C: periodic oscillation (no flapping) ##########"
reset 2
# total load alternates 180 <-> 80; per-pod value dilutes as replicas grow
post /v1/metrics "$(samples_json 2026-09-24T13:00:00Z $START 90 p1 p2)" >/dev/null
evaluate "C1 high (90/pod x2, ratio 1.8): scale up +2" 2026-09-24T13:00:00Z
post /v1/metrics "$(samples_json 2026-09-24T13:01:01Z $START 20 p1 p2 p3 p4)" >/dev/null
evaluate "C2 low (20/pod x4, ratio 0.4): window protects" 2026-09-24T13:01:01Z
post /v1/metrics "$(samples_json 2026-09-24T13:02:02Z $START 45 p1 p2 p3 p4)" >/dev/null
evaluate "C3 high again (45/pod x4, ratio 0.9): tolerance holds" 2026-09-24T13:02:02Z
post /v1/metrics "$(samples_json 2026-09-24T13:03:03Z $START 20 p1 p2 p3 p4)" >/dev/null
evaluate "C4 low again: still protected" 2026-09-24T13:03:03Z
post /v1/metrics "$(samples_json 2026-09-24T13:04:04Z $START 45 p1 p2 p3 p4)" >/dev/null
evaluate "C5 high again: no flapping" 2026-09-24T13:04:04Z

echo
echo "Done. Full decision history: curl $BASE/v1/decisions"
