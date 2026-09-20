#!/usr/bin/env bash
# End-to-end SIRCC walkthrough using only curl + python3.
#
#   ./examples/walkthrough.sh [base_url]
#
# Brings a P1 case from detection to closure, exercising:
#   P1 triage gate, role checks, optimistic version, evidence + correction,
#   action item scheduling, the overdue reminder, the closure gate and export.
#
# It assumes the server is running (make run / docker compose up) and the
# scheduler is active. Requires a POSIX shell, curl and python3.
set -euo pipefail

BASE="${1:-http://localhost:8080}"
ADMIN_KEY="sircc_demo_admin_0001"
ANALYST_KEY="sircc_demo_analyst_0001"
RESPONDER1_ID="11111111-1111-1111-1111-111111111301"

req() { # METHOD PATH KEY [JSON_BODY] [IDEMPOTENCY_KEY]
  local method="$1" path="$2" key="$3" body="${4:-}" idem="${5:-}"
  local args=(-s -X "$method" -H "X-API-Key: $key" -H 'Content-Type: application/json')
  [[ -n "$idem" ]] && args+=(-H "Idempotency-Key: $idem")
  [[ -n "$body" ]] && args+=(-d "$body")
  curl "${args[@]}" "$BASE$path"
}
code() { # same args as req but print HTTP status
  local method="$1" path="$2" key="$3" body="${4:-}" idem="${5:-}"
  local args=(-s -o /dev/null -w '%{http_code}' -X "$method" -H "X-API-Key: $key" -H 'Content-Type: application/json')
  [[ -n "$idem" ]] && args+=(-H "Idempotency-Key: $idem")
  [[ -n "$body" ]] && args+=(-d "$body")
  curl "${args[@]}" "$BASE$path"
}
jget() { python3 -c "import sys,json;print(json.load(sys.stdin)$1)"; }

echo ">> health"
curl -s "$BASE/healthz"; echo

echo ">> create P1 incident as analyst1"
INCIDENT=$(req POST /api/v1/incidents "$ANALYST_KEY" \
  '{"title":"Credential stuffing against VPN","description":"failed logins x40k","severity":"P1"}' \
  | jget "['id']")
echo "   incident=$INCIDENT  version=1"

echo ">> triage with no responder assigned -> expect 422"
echo "   $(code POST "/api/v1/incidents/$INCIDENT/transitions" "$ANALYST_KEY" \
  '{"action":"triage","expected_version":1}')"

echo ">> responder tries to self-assign -> expect 403"
echo "   $(code PUT "/api/v1/incidents/$INCIDENT/members/$RESPONDER1_ID" "$ANALYST_KEY" \
  '{"case_role":"responder"}')"

echo ">> admin assigns responder1 -> expect 200"
echo "   $(code PUT "/api/v1/incidents/$INCIDENT/members/$RESPONDER1_ID" "$ADMIN_KEY" \
  '{"case_role":"responder"}')"

echo ">> triage v1 (same idempotency key twice: second is a replay)"
KEY=$(python3 -c 'import uuid;print(uuid.uuid4())')
echo "   first=$(code POST "/api/v1/incidents/$INCIDENT/transitions" "$ANALYST_KEY" \
  '{"action":"triage","expected_version":1}' "$KEY") replay=$(code POST "/api/v1/incidents/$INCIDENT/transitions" "$ANALYST_KEY" \
  '{"action":"triage","expected_version":1}' "$KEY") (both 200; second carries Idempotent-Replay)"

echo ">> analyst adds evidence, then a correction note"
EVID=$(req POST "/api/v1/incidents/$INCIDENT/evidence" "$ANALYST_KEY" \
  '{"content":"source IPs concentrated in AS64500"}' | jget "['id']")
req POST "/api/v1/incidents/$INCIDENT/evidence/$EVID/notes" "$ANALYST_KEY" \
  '{"note":"correction: ASN is AS65500, typo in first note"}' >/dev/null
echo "   evidence=$EVID (immutable; correction appended)"

echo ">> create an action item due in ~2 seconds"
DUE=$(python3 -c "import datetime;print((datetime.datetime.now(datetime.UTC)+datetime.timedelta(seconds=2)).strftime('%Y-%m-%dT%H:%M:%SZ'))")
ITEM=$(req POST "/api/v1/incidents/$INCIDENT/action-items" "$ANALYST_KEY" \
  "{\"description\":\"Block ASN at perimeter\",\"owner_user_id\":\"$RESPONDER1_ID\",\"due_at\":\"$DUE\"}" \
  | jget "['id']")
echo "   action_item=$ITEM due_version=1"

echo ">> responder drives contain(v2) -> eradicate(v3) -> recover(v4)"
code POST "/api/v1/incidents/$INCIDENT/transitions" "sircc_demo_responder_0001" \
  '{"action":"contain","expected_version":2}' >/dev/null
code POST "/api/v1/incidents/$INCIDENT/transitions" "sircc_demo_responder_0001" \
  '{"action":"eradicate","expected_version":3}' >/dev/null
code POST "/api/v1/incidents/$INCIDENT/transitions" "sircc_demo_responder_0001" \
  '{"action":"recover","expected_version":4}' >/dev/null
echo "   advanced to recovered"

echo ">> analyst performs review(v5)"
code POST "/api/v1/incidents/$INCIDENT/transitions" "$ANALYST_KEY" \
  '{"action":"review","expected_version":5}' >/dev/null

echo ">> close with no artifacts -> expect 422"
echo "   $(code POST "/api/v1/incidents/$INCIDENT/transitions" "$ADMIN_KEY" \
  '{"action":"close","expected_version":6}')"

echo ">> waiting 4s for the scheduler to persist the overdue reminder..."
sleep 4
req GET "/api/v1/incidents/$INCIDENT/action-items" "$ADMIN_KEY" \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print('   reminders persisted:',len(d['reminders']))"

echo ">> close with root cause + lessons (action item already present) -> 200"
req POST "/api/v1/incidents/$INCIDENT/transitions" "$ADMIN_KEY" \
  '{"action":"close","expected_version":6,"root_cause":"No rate limiting on VPN auth endpoint; credential stuffing succeeded for 3 accounts.","lessons_learned":"Add geo-velocity and failure-rate controls; rotate the 3 credentials; tabletop the runbook."}' \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print('   status=%s version=%s closed_at=%s'%(d['status'],d['version'],d.get('closed_at')))"

echo ">> export"
req GET "/api/v1/incidents/$INCIDENT/export" "$ADMIN_KEY" \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print('   phases:',[p['phase'] for p in d['phases']]);print('   ttc_s:',d['metrics']['time_to_containment_seconds']);print('   evidence:',[(e['seq'],e['note_count']) for e in d['evidence_summary']])"

echo
echo "Walkthrough complete. Inspect the full export at:"
echo "  curl -s -H 'X-API-Key: $ADMIN_KEY' $BASE/api/v1/incidents/$INCIDENT/export | python3 -m json.tool"
