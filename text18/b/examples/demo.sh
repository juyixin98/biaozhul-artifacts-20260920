#!/usr/bin/env bash
# End-to-end SIRCC walkthrough. Requires a running stack (make up) and curl + jq.
set -euo pipefail

BASE="${BASE:-http://localhost:8080}"
A="u-analyst"; R="u-responder"; M="u-admin"

req() { # user request_id METHOD path [json]
  local user="$1" rid="$2" method="$3" path="$4" body="${5:-}"
  if [[ -n "$body" ]]; then
    curl -sS -X "$method" "$BASE$path" \
      -H "X-User-Id: $user" -H "X-Request-Id: $rid" \
      -H 'Content-Type: application/json' -d "$body"
  else
    curl -sS -X "$method" "$BASE$path" -H "X-User-Id: $user"
  fi
}

echo "== health =="
curl -sS "$BASE/healthz"; echo

echo "== 1. Analyst opens a P1 incident =="
INC=$(req "$A" "demo-create-1" POST /v1/incidents \
  '{"title":"Credential phishing wave","severity":"P1"}' | tee /dev/stderr | jq -r .id)
echo "incident: $INC"

echo "== 2. Triage blocked: P1 has no responder =="
req "$A" "demo-triage-block" POST "/v1/incidents/$INC/transition" \
  '{"expected_version":1}'; echo

echo "== 3. Admin assigns a responder =="
req "$M" "demo-assign-r" POST "/v1/incidents/$INC/assignments" \
  "{\"user_id\":\"$R\",\"role\":\"responder\"}" >/dev/null && echo assigned

echo "== 4. Analyst completes triage (v1 -> v2) =="
req "$A" "demo-triage-ok" POST "/v1/incidents/$INC/transition" \
  '{"expected_version":1,"note":"User clicked link, creds entered"}' | jq '{stage,version}'

echo "== 5. Analyst attaches immutable evidence + correction =="
EV=$(req "$A" "demo-ev-1" POST "/v1/incidents/$INC/evidence" \
  '{"content":"Email headers point at lookalike domain"}' | jq -r .id)
req "$A" "demo-ev-note-1" POST "/v1/incidents/$INC/evidence/$EV/notes" \
  '{"content":"Correction: SPF result was softfail, not fail"}' >/dev/null && echo "note appended"

echo "== 6. Repeated request id returns the original result =="
req "$A" "demo-ev-1" POST "/v1/incidents/$INC/evidence" \
  '{"content":"duplicate attempt"}' | jq '{id,content}'

echo "== 7. Responder drives containment, eradication, recovery =="
req "$R" "demo-contain" POST "/v1/incidents/$INC/transition" '{"expected_version":2}' | jq '{stage,version}'
req "$R" "demo-erad"   POST "/v1/incidents/$INC/transition" '{"expected_version":3}' | jq '{stage,version}'
req "$R" "demo-recov"  POST "/v1/incidents/$INC/transition" '{"expected_version":4}' | jq '{stage,version}'

echo "== 8. Analyst records root cause + lessons (postmortem) =="
req "$A" "demo-pm" POST "/v1/incidents/$INC/transition" \
  '{"expected_version":5,"root_cause":"Lookalike domain + no MFA","lessons_learned":"Enforce MFA and mail filtering"}' \
  | jq '{stage,version}'

echo "== 9. Close blocked: no action item =="
req "$R" "demo-close-block" POST "/v1/incidents/$INC/transition" \
  '{"expected_version":6}' ; echo

echo "== 10. Create and complete an action item, then close =="
ITEM=$(req "$A" "demo-ai-1" POST "/v1/incidents/$INC/action-items" \
  "{\"title\":\"Block domain at proxy\",\"owner_id\":\"$R\",\"due_at\":\"2030-01-01T00:00:00Z\"}" | jq -r .id)
req "$R" "demo-ai-done" POST "/v1/action-items/$ITEM/status" '{"status":"done"}' >/dev/null && echo "item done"
req "$R" "demo-close-ok" POST "/v1/incidents/$INC/transition" '{"expected_version":6}' \
  | jq '{stage,version,closed_at}'

echo "== 11. Export: stages, evidence, durations =="
req "$A" "" GET "/v1/incidents/$INC/export" \
  | jq '{stage: .incident.stage, durations, events: (.stage_events|length), evidence: (.evidence_summary|length)}'
