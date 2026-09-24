#!/usr/bin/env bash
# End-to-end acceptance walkthrough for the task resource deadlock checker.
#
# Demonstrates, against a freshly started server:
#   1. atomic acquisition + partial-failure non-blocking
#   2. cross-resource (AB-BA) dynamic requests and cycle rejection (409)
#   3. timeout -> uncertain fence (resources retained, next task blocked)
#   4. late completion after timeout -> grant wave unblocks the waiter
#   5. confirmed-stop revocation releasing only asserted resources
#   6. priority aging (a low-priority waiter overtakes a fresh high-priority one)
#   7. tamper-evident HMAC evidence ledger
#
# Usage:
#   ./examples/demo.sh            # expects server on $BASE (default :8080)
#   BASE=http://localhost:9000 ./examples/demo.sh
set -euo pipefail

BASE="${BASE:-http://localhost:8080}"
API="$BASE/api/v1"

bold() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }
req() { # method path [json-file]
  local method="$1" path="$2" body="${3:-}"
  if [[ -n "$body" ]]; then
    curl -sS -X "$method" -H 'Content-Type: application/json' \
      --data-binary "@$body" "$API$path"
  else
    curl -sS -X "$method" "$API$path"
  fi
}
jqr() { # compact JSON pretty printer; keeps a leading "HTTP nnn" status line
  if command -v python3 >/dev/null; then
    python3 -c '
import sys,json
raw=sys.stdin.read()
lines=raw.split("\n",1)
head=lines[0] if lines and lines[0].startswith("HTTP ") else ""
body=lines[1] if head else raw
if head: print(head)
try:
    print(json.dumps(json.loads(body),indent=2))
except Exception:
    print(body)
'
  else cat; fi
}
code_req() { # like req but prints "<status>\n<body>"
  local method="$1" path="$2" body="${3:-}" tmp
  tmp="$(mktemp)"
  if [[ -n "$body" ]]; then
    code=$(curl -sS -o "$tmp" -w '%{http_code}' -X "$method" \
      -H 'Content-Type: application/json' --data-binary "@$body" "$API$path")
  else
    code=$(curl -sS -o "$tmp" -w '%{http_code}' -X "$method" "$API$path")
  fi
  echo "HTTP $code"
  cat "$tmp"
  rm -f "$tmp"
}
id_of() { python3 -c 'import sys,json;print(json.load(sys.stdin)["task_id"])'; }

bold "health"
curl -sS "$BASE/healthz" | jqr

bold "1. register resources"
req POST /resources <(cat <<'JSON'
{"resources":[
 {"kind":"tool","name":"drill"},
 {"kind":"tool","name":"welder"},
 {"kind":"tool","name":"gripper"},
 {"kind":"station","name":"bay-1"},
 {"kind":"station","name":"bay-2"}]}
JSON
) | jqr

bold "2. task A atomically takes drill + bay-1 (expect 201 granted)"
RESP_A="$(req POST /tasks <(cat <<'JSON'
{"label":"A","priority":100,"timeout_ms":30000,"resources":[
 {"kind":"tool","name":"drill"},{"kind":"station","name":"bay-1"}]}
JSON
))"
echo "$RESP_A" | jqr
A=$(echo "$RESP_A" | id_of)

bold "3. task B wants drill+welder: welder free but drill held -> 202 waiting, holds NOTHING"
RESP_B="$(req POST /tasks <(cat <<'JSON'
{"label":"B","priority":90,"timeout_ms":30000,"resources":[
 {"kind":"tool","name":"drill"},{"kind":"tool","name":"welder"}]}
JSON
))"
echo "$RESP_B" | jqr
B=$(echo "$RESP_B" | id_of)
echo "B waits on: $(echo "$RESP_B" | python3 -c 'import sys,json;d=json.load(sys.stdin);print([(r["resource"]["kind"]+"/"+r["resource"]["name"],"holder task",r["holder_task_id"]) for r in d.get("wait_reasons",[])])')"

bold "4. task C takes welder+bay-2 to build a 3-way setup, C starts (201)"
RESP_C="$(req POST /tasks <(cat <<'JSON'
{"label":"C","priority":100,"timeout_ms":30000,"resources":[
 {"kind":"tool","name":"welder"},{"kind":"station","name":"bay-2"}]}
JSON
))"
echo "$RESP_C" | jqr
C=$(echo "$RESP_C" | id_of)
echo "NOTE: C can only start if welder+bay-2 are free; B holds nothing so this is fine"

bold "5. A dynamically requests welder (held by C): edge A->C, A waits (202)"
req POST "/tasks/$A/requests" <(cat <<'JSON'
{"resources":[{"kind":"tool","name":"welder"}]}
JSON
) | jqr

bold "6. C dynamically requests drill (held by A): would close A->C->A -> 409 cycle_detected"
code_req POST "/tasks/$C/requests" <(cat <<'JSON'
{"resources":[{"kind":"tool","name":"drill"}]}
JSON
) | jqr

bold "7. current wait-for graph (one edge A->C, no cycles)"
req GET /debug/waits | jqr

bold "8. resource holding evidence: GET /holds"
req GET /holds | jqr

bold "9. timeout fence: create SHORT-deadline D holding gripper, E waits for it"
req POST /resources <(cat <<'JSON'
{"resources":[{"kind":"tool","name":"gripper"}]}
JSON
) >/dev/null
RESP_D="$(req POST /tasks <(cat <<'JSON'
{"label":"D-short","priority":100,"timeout_ms":400,"resources":[
 {"kind":"tool","name":"gripper"}]}
JSON
))"
D=$(echo "$RESP_D" | id_of)
RESP_E="$(req POST /tasks <(cat <<'JSON'
{"label":"E-wait","priority":100,"timeout_ms":30000,"resources":[
 {"kind":"tool","name":"gripper"}]}
JSON
))"
E=$(echo "$RESP_E" | id_of)
echo "waiting for D's 400ms deadline ..."; sleep 0.6
bold "9a. run sweep -> D transitions to uncertain"
req POST /admin/sweep <<< '{}' | jqr
bold "9b. E must STILL be waiting (uncertain D keeps gripper)"
req GET "/tasks/$E" | jqr

bold "10. D's late completion (confirmed stop) -> grant wave hands gripper to E"
req POST "/tasks/$D/complete" <<< '{}' | jqr
req GET "/tasks/$E" | jqr

bold "11. confirmed-stop revoke: C releases welder only (asserted stopped);"
bold "    the grant wave then satisfies A's pending welder request"
req POST "/tasks/$C/revoke" <(cat <<'JSON'
{"resources":[{"kind":"tool","name":"welder"}],"reason":"welder confirmed stopped"}
JSON
) | jqr
echo "-- A should now hold drill + welder:"
req GET "/tasks/$A" | python3 -c 'import sys,json;d=json.load(sys.stdin);print("A state:",d["state"],"holds:",sorted((r["kind"],r["name"]) for r in d["held_resources"]))'

bold "12. priority aging on a fresh resource 'press'"
req POST /resources <(cat <<'JSON'
{"resources":[{"kind":"tool","name":"press"}]}
JSON
) >/dev/null
H=$(req POST /tasks <(cat <<'JSON'
{"label":"H-holder","priority":500,"timeout_ms":60000,"resources":[{"kind":"tool","name":"press"}]}
JSON
) | id_of)
L=$(req POST /tasks <(cat <<'JSON'
{"label":"L-old-lowprio","priority":10,"aging_per_sec":100,"timeout_ms":60000,"resources":[{"kind":"tool","name":"press"}]}
JSON
) | id_of)
Q=$(req POST /tasks <(cat <<'JSON'
{"label":"Q-new-highprio","priority":200,"aging_per_sec":0,"timeout_ms":60000,"resources":[{"kind":"tool","name":"press"}]}
JSON
) | id_of)
echo "freshman Q (priority 200) initially outranks L (priority 10):"
req GET "/tasks/$L" | python3 -c 'import sys,json;d=json.load(sys.stdin);print("L effective=%.1f queue=#%d"%(d["effective_priority"],d["queue_position"]))'
req GET "/tasks/$Q" | python3 -c 'import sys,json;d=json.load(sys.stdin);print("Q effective=%.1f queue=#%d"%(d["effective_priority"],d["queue_position"]))'
echo "sleeping 2.2s so L ages by ~220 priority points ..."; sleep 2.2
req GET "/tasks/$L" | python3 -c 'import sys,json;d=json.load(sys.stdin);print("L effective=%.1f queue=#%d (aged to head)"%(d["effective_priority"],d["queue_position"]))'
req GET "/tasks/$Q" | python3 -c 'import sys,json;d=json.load(sys.stdin);print("Q effective=%.1f queue=#%d"%(d["effective_priority"],d["queue_position"]))'

bold "13. signed evidence ledger (newest first; every record valid=true)"
req GET "/evidence?limit=6" | python3 -c '
import sys,json
for e in json.load(sys.stdin):
    print("id=%-2s event=%-10s task=%-3s valid=%s" % (e["id"], e["event"], e["task_id"], e["valid"]))
'

bold "demo complete"
echo "Inspect any task:  curl -sS $API/tasks/<id>"
echo "Inspect the graph: curl -sS $API/debug/waits"
echo "HMAC key persists in service_meta so evidence verifies across restarts."
