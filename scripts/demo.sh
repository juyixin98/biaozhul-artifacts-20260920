#!/usr/bin/env bash
# Full end-to-end walkthrough of the CommunityVault moderation engine.
#
# Demonstrates: draft -> submit -> (auto-reject) -> clean submit ->
# claim -> approve -> publish -> feed -> duplicate report -> rule switch ->
# edit invalidates stale approval -> rollback -> withdraw -> audit trail.
#
# Usage: scripts/demo.sh [base_url]
set -u

BASE="${1:-http://localhost:8080}"
H_ALICE=(-H 'X-User-ID: 1' -H 'Content-Type: application/json')
H_BOB=(-H 'X-User-ID: 2' -H 'Content-Type: application/json')
H_TECH=(-H 'X-User-ID: 3' -H 'Content-Type: application/json')
H_ART=(-H 'X-User-ID: 4' -H 'Content-Type: application/json')
H_ADMIN=(-H 'X-User-ID: 5' -H 'Content-Type: application/json')

say() { printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }
pp() { python3 -m json.tool 2>/dev/null || cat; }

say "health"
curl -s "$BASE/healthz"; echo

say "1. alice creates a DRAFT (revision #1)"
CREATE=$(curl -s "${H_ALICE[@]}" -X POST "$BASE/v1/contents" \
  -d '{"category":"tech","title":"My first post","body":"hello everyone","edit_reason":"initial"}')
echo "$CREATE" | pp
CID=$(echo "$CREATE" | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')

say "2. bob cannot see alice's draft (404, existence hidden)"
curl -s -o /dev/null -w 'HTTP %{http_code}\n' "${H_BOB[@]}" "$BASE/v1/contents/$CID"

say "3. alice submits a body containing a forbidden word -> 422 + auto-rejection evidence"
curl -s "${H_ALICE[@]}" -X POST "$BASE/v1/contents/$CID/edits" \
  -d '{"title":"My first post","body":"this is forbidden content","edit_reason":"add bad word"}' | pp >/dev/null
curl -s -w '\nHTTP %{http_code}\n' "${H_ALICE[@]}" -X POST "$BASE/v1/contents/$CID/submit"

say "4. alice restores a clean body and resubmits -> pending"
curl -s "${H_ALICE[@]}" -X POST "$BASE/v1/contents/$CID/edits" \
  -d '{"title":"My first post","body":"hello everyone, fixed","edit_reason":"remove bad word"}' | pp >/dev/null
curl -s "${H_ALICE[@]}" -X POST "$BASE/v1/contents/$CID/submit" | pp

say "5. art moderator cannot claim the tech task (204); tech moderator claims it"
curl -s -o /dev/null -w 'art mod claim: HTTP %{http_code}\n' "${H_ART[@]}" -X POST "$BASE/v1/mod/claims"
CLAIM=$(curl -s "${H_TECH[@]}" -X POST "$BASE/v1/mod/claims")
echo "$CLAIM" | pp
TID=$(echo "$CLAIM" | python3 -c 'import sys,json;print(json.load(sys.stdin)["task_id"])')

say "6. tech moderator rejects without a reason -> 400; then approves -> published"
curl -s -o /dev/null -w 'reject no reason: HTTP %{http_code}\n' "${H_TECH[@]}" \
  -X POST "$BASE/v1/mod/tasks/$TID/reject" -d '{"reason":""}'
curl -s "${H_TECH[@]}" -X POST "$BASE/v1/mod/tasks/$TID/approve" \
  -d '{"reason":"looks fine"}' | pp

say "7. bob now sees it in the public feed"
curl -s "${H_BOB[@]}" "$BASE/v1/contents?limit=5" | pp

say "8. bob reports twice: first 201 created=true, second 200 created=false (no count bump)"
curl -s -w '\nHTTP %{http_code}\n' "${H_BOB[@]}" -X POST "$BASE/v1/contents/$CID/reports" \
  -d '{"reason":"looks like spam"}'
curl -s -w '\nHTTP %{http_code}\n' "${H_BOB[@]}" -X POST "$BASE/v1/contents/$CID/reports" \
  -d '{"reason":"reporting again"}'

say "9. alice sees only aggregate report counts; moderator sees reporter identity"
curl -s "${H_ALICE[@]}" "$BASE/v1/contents/$CID/reports"; echo
curl -s "${H_TECH[@]}" "$BASE/v1/contents/$CID/reports"; echo

say "10. admin publishes rule v2 adding 'zap'; alice withdraws, edits with zap, resubmits -> 422 under v2"
curl -s "${H_ADMIN[@]}" -X POST "$BASE/v1/rules" \
  -d '{"description":"add zap","words":["zap"]}' | pp
curl -s "${H_ALICE[@]}" -X POST "$BASE/v1/contents/$CID/withdraw" \
  -d '{"reason":"need to update"}' | pp >/dev/null
curl -s "${H_ALICE[@]}" -X POST "$BASE/v1/contents/$CID/edits" \
  -d '{"title":"My first post","body":"zap! new version","edit_reason":"rewrite"}' | pp >/dev/null
curl -s -o /dev/null -w 'submit with zap under v2: HTTP %{http_code}\n' "${H_ALICE[@]}" \
  -X POST "$BASE/v1/contents/$CID/submit"

say "11. STALE APPROVAL: despite the earlier approval, rev3 is NOT published"
curl -s "${H_ALICE[@]}" "$BASE/v1/contents/$CID" | python3 -c 'import sys,json;d=json.load(sys.stdin);print("status =",d["status"],"revision =",d["current_revision"]["revision_no"])'

say "12. alice rolls back to a clean revision (new revision #4, history preserved)"
REV1=$(curl -s "${H_ALICE[@]}" "$BASE/v1/contents/$CID/revisions" | python3 -c 'import sys,json;print(json.load(sys.stdin)[-1]["id"])')
curl -s "${H_ALICE[@]}" -X POST "$BASE/v1/contents/$CID/rollback" \
  -d "{\"revision_id\":$REV1,\"reason\":\"undo the zap experiment\"}" | pp

say "13. audit trail: status events and moderation decisions"
curl -s "${H_TECH[@]}" "$BASE/v1/contents/$CID/events" | pp | head -40
curl -s "${H_TECH[@]}" "$BASE/v1/contents/$CID/decisions" | pp | head -40

echo
echo "Demo complete. Content id was $CID."
