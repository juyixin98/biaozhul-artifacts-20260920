#!/usr/bin/env bash
# demo.sh — full happy-path: ingest v1 → promote to staging → ingest v2 →
# promote → rollback to v1. Requires the server on :8080 and jq.
set -euo pipefail
BASE="${BASE:-http://localhost:8080}"
cd "$(dirname "$0")"

say() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }

say "create environments"
curl -sf -XPOST "$BASE/v1/envs" -d @env-test.json   | jq '{name, generation}'
curl -sf -XPOST "$BASE/v1/envs" -d @env-staging.json | jq '{name, generation}'

ingest() { # env, content, expected_generation -> digest
  curl -sf -XPOST "$BASE/v1/envs/$1/artifacts?expected_generation=$3" \
    --data-binary "$2" | jq -r '.artifact_digest'
}

say "ingest v1 into test (generation 0)"
D1=$(ingest test "payment-svc build 1" 0)
echo "digest: $D1"

say "register evidence v1 + policy v1"
jq --arg d "$D1" '.artifact_digest=$d' evidence.json | curl -sf -XPOST "$BASE/v1/evidence" -d @- | jq '{id, version, passed}'
curl -sf -XPOST "$BASE/v1/policies" -d @policy.json | jq '{id, version}'

say "approve v1 for staging (binds current generation)"
A1=$(jq --arg d "$D1" '.artifact_digest=$d' approval.json | curl -sf -XPOST "$BASE/v1/approvals" -d @- | jq -r '.id')
echo "approval: $A1"

say "promote v1 test → staging (expected_generation 0)"
jq --arg d "$D1" --arg a "$A1" '.digest=$d | .approval_id=$a' promotion.json \
  | curl -sf -XPOST "$BASE/v1/promotions" -d @- | jq '{status, steps: [.steps[].name]}'

say "ingest v2 + promote (expected_generation 1)"
D2=$(ingest test "payment-svc build 2" 1)
jq --arg d "$D2" '.artifact_digest=$d' evidence.json | curl -sf -XPOST "$BASE/v1/evidence" -d @- >/dev/null
A2=$(jq --arg d "$D2" '.artifact_digest=$d' approval.json | curl -sf -XPOST "$BASE/v1/approvals" -d @- | jq -r '.id')
jq --arg d "$D2" --arg a "$A2" '.digest=$d | .approval_id=$a | .evidence_version=2 | .expected_generation=1' promotion.json \
  | curl -sf -XPOST "$BASE/v1/promotions" -d @- | jq '{status}'

say "staging now"
curl -sf "$BASE/v1/envs/staging" | jq '{current_digest, generation}'

say "rollback staging → v1 (expected_generation 2)"
jq --arg d "$D1" '.digest=$d | .expected_generation=2' rollback.json \
  | curl -sf -XPOST "$BASE/v1/rollbacks" -d @- | jq '{status, kind}'

say "staging after rollback"
curl -sf "$BASE/v1/envs/staging" | jq '{current_digest, generation}'

say "history with rollback-candidate evaluation"
curl -sf "$BASE/v1/envs/staging/history" | jq '.[] | {generation, digest: .digest[:19], current, complete, retention_ok}'
