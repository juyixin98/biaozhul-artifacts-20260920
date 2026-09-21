#!/usr/bin/env bash
# End-to-end demo of the SIRCC API against a locally running server.
# Usage: ./samples/demo.sh [base_url]
set -euo pipefail

BASE="${1:-http://localhost:8080}"
ADMIN=(-H "X-User-ID: admin1" -H "X-User-Role: admin")
ANA=(-H "X-User-ID: ana1" -H "X-User-Role: analyst")
RES=(-H "X-User-ID: res1" -H "X-User-Role: responder")

j() { python3 -c "import sys,json;d=json.load(sys.stdin);print(eval('d'+sys.argv[1]))" "$1"; }

echo "== create P1 incident =="
INC=$(curl -sf "${ADMIN[@]}" -X POST "$BASE/incidents" \
  -d '{"title":"Suspicious OAuth grant storm","severity":"P1"}')
ID=$(echo "$INC" | j "['id']")
echo "incident $ID"

echo "== admin assigns the analyst =="
curl -sf "${ADMIN[@]}" -X POST "$BASE/incidents/$ID/members" -d '{"user_id":"ana1","role":"analyst"}' >/dev/null
echo ok

echo "== P1 triage blocked without responder (expect GATE_UNMET) =="
curl -s "${ANA[@]}" -X POST "$BASE/incidents/$ID/transitions" \
  -d '{"to":"triaged","expected_version":1,"request_id":"demo-t1"}'; echo

echo "== admin assigns the responder =="
curl -sf "${ADMIN[@]}" -X POST "$BASE/incidents/$ID/members" -d '{"user_id":"res1","role":"responder"}' >/dev/null
echo ok

echo "== walk the lifecycle (idempotent request ids) =="
V=1
step() { # to, actor headers..., request id
  local to="$1"; shift
  local req="$1"; shift
  local out
  out=$(curl -sf "$@" -X POST "$BASE/incidents/$ID/transitions" \
    -d "{\"to\":\"$to\",\"expected_version\":$V,\"request_id\":\"$req\"}")
  V=$(echo "$out" | j "['incident']['version']")
  echo "  -> $to (version $V)"
}
step triaged   demo-t2 "${ANA[@]}"
step contained demo-t3 "${RES[@]}"
step eradicated demo-t4 "${RES[@]}"
step recovered demo-t5 "${RES[@]}"
step postmortem demo-t6 "${RES[@]}"

echo "== replay demo-t2 returns the stored result =="
curl -s "${ANA[@]}" -X POST "$BASE/incidents/$ID/transitions" \
  -d '{"to":"triaged","expected_version":1,"request_id":"demo-t2"}'; echo

echo "== analyst submits evidence + a correction note =="
EV=$(curl -sf "${ANA[@]}" -X POST "$BASE/incidents/$ID/evidence" \
  -d '{"content":"IdP log: 412 grant requests from 198.51.100.7 within 60s"}')
EVID=$(echo "$EV" | j "['id']")
curl -sf "${ANA[@]}" -X POST "$BASE/evidence/$EVID/notes" \
  -d '{"note":"correction: source IP is in the 198.51.100.0/24 TEST-NET-2 range"}' >/dev/null
echo ok

echo "== responder adds an action item (due in 3 days) =="
DUE=$(date -u -d '+3 days' +%Y-%m-%dT%H:%M:%SZ)
curl -sf "${RES[@]}" -X POST "$BASE/incidents/$ID/action-items" \
  -d "{\"title\":\"tighten OAuth consent policy\",\"owner_id\":\"res1\",\"due_at\":\"$DUE\"}" >/dev/null
echo ok

echo "== close blocked until postmortem is recorded (expect GATE_UNMET) =="
curl -s "${RES[@]}" -X POST "$BASE/incidents/$ID/transitions" \
  -d "{\"to\":\"closed\",\"expected_version\":$V,\"request_id\":\"demo-c1\"}"; echo

echo "== record postmortem, then close =="
curl -sf "${RES[@]}" -X PUT "$BASE/incidents/$ID/postmortem" \
  -d '{"root_cause":"overly permissive OAuth consent screen","lessons_learned":"require admin approval for new scopes"}' >/dev/null
V=$((V + 1))
curl -sf "${RES[@]}" -X POST "$BASE/incidents/$ID/transitions" \
  -d "{\"to\":\"closed\",\"expected_version\":$V,\"request_id\":\"demo-c2\"}" >/dev/null
echo "closed"

echo "== export =="
curl -sf "${RES[@]}" "$BASE/incidents/$ID/export" | python3 -m json.tool
