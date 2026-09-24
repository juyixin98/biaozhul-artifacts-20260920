#!/usr/bin/env bash
# End-to-end acceptance for the CRD migration compatibility project.
#
# Brings up the local envtest-backed environment, runs the same checks a
# reviewer would run by hand against a real kube-apiserver, and tears it all
# down. Exits non-zero on the first failed assertion.
#
# Usage:  make acceptance     (sets KUBEBUILDER_ASSETS)
#    or:  bash hack/acceptance.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

: "${KUBEBUILDER_ASSETS:?set KUBEBUILDER_ASSETS, e.g. via 'make acceptance'}"
# dev-env writes its kubeconfig to a well-known path.
LOG=$(mktemp)
PASS=0; FAIL=0

ok()   { echo "  PASS: $1"; PASS=$((PASS+1)); }
bad()  { echo "  FAIL: $1"; FAIL=$((FAIL+1)); }
assert() { if [ "$1" = "$2" ]; then ok "$3"; else bad "$3 (want '$1' got '$2')"; fi; }

cleanup() {
  [ -n "${DEV_PID:-}" ] && kill "${DEV_PID}" 2>/dev/null || true
}
trap cleanup EXIT

echo "== building dev-env =="
go build -o /tmp/dev-env-accept ./cmd/dev-env
go build -o /tmp/storage-migrate-accept ./cmd/storage-migrate

echo "== starting local control plane + webhooks =="
( /tmp/dev-env-accept >"$LOG" 2>&1 ) &
DEV_PID=$!

for _ in $(seq 1 60); do
  grep -q "dev-env is ready" "$LOG" && break
  sleep 1
done
grep -q "dev-env is ready" "$LOG" || { echo "dev-env failed to start"; cat "$LOG"; exit 1; }
export KUBECONFIG=/tmp/crd-migrate-dev-kubeconfig

echo "== 1. legacy create + structured v1 read =="
kubectl apply -f test/fixtures/task-v1alpha1.yaml >/dev/null
SECS=$(kubectl get tasks.v1alpha1.migration.example.io nightly-backup -o jsonpath='{.spec.timeoutSeconds}')
assert "120" "$SECS" "integer seconds stored on legacy view"
V1TO=$(kubectl get tasks.v1.migration.example.io nightly-backup -o jsonpath='{.spec.timeout.seconds}')
assert "120" "$V1TO" "server converts integer seconds to structured duration"

echo "== 2. v1 new fields defaulted on create =="
cat <<'YAML' | kubectl apply -f - >/dev/null
apiVersion: migration.example.io/v1
kind: Task
metadata: {name: defaulted-task, namespace: default}
spec: {payload: p}
YAML
P=$(kubectl get tasks.v1.migration.example.io defaulted-task -o jsonpath='{.spec.priority}')
T=$(kubectl get tasks.v1.migration.example.io defaulted-task -o jsonpath='{.spec.timeout.seconds}')
assert "Normal" "$P" "priority defaults to Normal"
assert "30" "$T" "timeout defaults to 30s"

echo "== 3. inexpressible sub-second value is an error, not truncation =="
kubectl apply -f test/fixtures/task-v1.yaml >/dev/null
# kubectl exits non-zero for the expected error; '|| true' keeps set -e +
# pipefail from aborting before grep sees the message.
OUT3=$(kubectl get tasks.v1alpha1.migration.example.io latency-sensitive 2>&1 || true)
if echo "$OUT3" | grep -q "sub-second component"; then
  ok "sub-second duration rejected on legacy read with explicit error"
else
  bad "sub-second duration was served truncated: $OUT3"
fi

echo "== 4. old client update does not erase new v1 fields =="
kubectl get tasks.v1alpha1.migration.example.io nightly-backup -o json \
  | python3 -c "import sys,json;o=json.load(sys.stdin);o['spec']['payload']='old-client-edit';print(json.dumps(o))" \
  | kubectl replace --raw "/apis/migration.example.io/v1alpha1/namespaces/default/tasks/nightly-backup" -f - >/dev/null
PR=$(kubectl get tasks.v1.migration.example.io nightly-backup -o jsonpath='{.spec.priority}')
TG=$(kubectl get tasks.v1.migration.example.io nightly-backup -o jsonpath='{.spec.tags}')
PL=$(kubectl get tasks.v1.migration.example.io nightly-backup -o jsonpath='{.spec.payload}')
assert "High" "$PR" "priority survives old-client update"
assert '["backup","nightly"]' "$TG" "tags survive old-client update"
assert "old-client-edit" "$PL" "old-client's own edit is visible"

echo "== 5. invalid durations are rejected explicitly =="
OUT5=$(kubectl apply -f test/fixtures/invalid-duration-v1.yaml 2>&1 || true)
if echo "$OUT5" | grep -q "should be greater than or equal to 0"; then
  ok "negative/out-of-range durations rejected with field errors"
else
  bad "invalid durations not rejected: $OUT5"
fi

echo "== 6. storage migration rewrites storage and trims storedVersions =="
REC=$(mktemp)
/tmp/storage-migrate-accept --record-file "$REC" --trim-stored-versions=true >/dev/null
SV=$(kubectl get crd tasks.migration.example.io -o jsonpath='{.status.storedVersions}')
assert '["v1"]' "$SV" "CRD storedVersions trimmed to v1"
grep -q '"status":"migrated"' "$REC" && ok "migration records written (JSONL)" || bad "no migrated records"

echo
echo "RESULT: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
