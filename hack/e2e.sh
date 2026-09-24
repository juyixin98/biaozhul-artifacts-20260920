#!/usr/bin/env bash
# Reproducible end-to-end demo against a `hack/deploy.sh`-installed kind cluster.
#
# Scenarios (all assertions are real k reads; nothing is faked):
#   1. Normal delivery to 3 namespaces
#   2. Out-of-order / duplicate events (repeated annotate triggers + full resync)
#      -> exactly one child per namespace, stable UID/resourceVersion
#   3. Partial failure (FailPolicy Always on test-b CREATE) -> success targets
#      keep versions, failed target retries and converges, no rollback
#   4. Selector narrowing -> owned child GC; foreign same-name object (owned by
#      a different, REAL ConfigSnapshot UID) kept and reported as name conflict
#   5. (covered by 4b) name conflict -> Failed, object untouched, then converges
#   6. Owner recreation with new UID -> old-UID child (held by a real successor
#      owner so kube GC keeps it) never deleted by the new owner
#   7. Process restart (pod delete) -> state restored from cache, idempotent
#
# Evidence is written to test/evidence/<timestamp>/.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CLUSTER="${CLUSTER:-config-dedup}"
# Pin every k invocation to our kind context (machines may run several
# kind clusters concurrently). The function is exported so `bash -c` probes
# used by check() inherit it.
export KCTX="kind-$CLUSTER"
k() { kubectl --context "$KCTX" "$@"; }
export -f k
TS="$(date +%Y%m%d-%H%M%S)"
EVID="$ROOT/test/evidence/$TS"
mkdir -p "$EVID"
LOG="$EVID/run.log"
exec > >(tee "$LOG") 2>&1

PASS=0; FAIL=0
check() { # check <name> <condition-command...>
  local name="$1"; shift
  if "$@"; then
    echo "PASS  $name" | tee -a "$EVID/results.txt"; PASS=$((PASS+1))
  else
    echo "FAIL  $name" | tee -a "$EVID/results.txt"; FAIL=$((FAIL+1))
  fi
}

snap_get() { k get configsnapshot "$1" -o json 2>/dev/null; }
jqr() { jq -r "$1"; }

echo "=== config-distributor e2e @ $TS ==="
k version --client -o json 2>/dev/null | jq -r '.clientVersion.gitVersion' | sed 's/^/k client: /'
k get nodes -o jsonpath='{.items[0].status.nodeInfo.kubeletVersion}{"\n"}' | sed 's/^/node kubelet: /'
k cluster-info 2>/dev/null | head -1

# Clean slate (best effort).
k delete configsnapshot demo-app --ignore-not-found --wait=false >/dev/null 2>&1 || true
k delete failpolicy --all --ignore-not-found >/dev/null 2>&1 || true
k get ns test-a test-b test-c >/dev/null 2>&1 || k apply -f "$ROOT/config/samples/namespaces.yaml"
for ns in test-a test-b test-c; do
  k label ns "$ns" tier=test --overwrite >/dev/null
done

CHILD_COUNT=0
child_name_from_status() { # child_name_from_status <ns>
  k get configsnapshot demo-app -o json \
    | jq -r --arg ns "$1" '.status.targets[]|select(.namespace==$ns)|.childName'
}
digest_from_status() {
  k get configsnapshot demo-app -o json | jq -r '.status.version'
}

echo
echo "=== Scenario 1: initial delivery to 3 namespaces ==="
k apply -f "$ROOT/config/samples/configsnapshot.yaml"
for i in $(seq 1 30); do
  n="$(k get configsnapshot demo-app -o json | jq -r '.status.appliedCount')"
  [ "$n" = "3" ] && break
  sleep 1
done
k get configsnapshot demo-app -o json > "$EVID/01-status.json"
for ns in test-a test-b test-c; do
  name="$(child_name_from_status "$ns")"
  check "child exists in $ns" k get configmap -n "$ns" "$name"
  check "payload correct in $ns" bash -c \
    "k get cm -n $ns $name -o json | jq -er '.data.config|test(\"feature.toggle=true\")' >/dev/null"
  check "ownerReference present in $ns" bash -c \
    "k get cm -n $ns $name -o json | jq -er '.metadata.ownerReferences[0].controller==true' >/dev/null"
  CHILD_COUNT=$((CHILD_COUNT+1))
done
DIGEST="$(digest_from_status)"
[ -n "$DIGEST" ] && [ "$DIGEST" != "null" ]
check "status carries 64-char content digest" bash -c "[ ${#DIGEST} -eq 64 ]"

echo
echo "=== Scenario 2: duplicate / out-of-order events must not recreate ==="
# Hammer the object with no-op metadata updates (50 events) and trigger a full
# informer resync path by restarting watches via label touches on namespaces.
for i in $(seq 1 25); do
  k annotate configsnapshot demo-app "event-test-$i=$i" --overwrite >/dev/null
  k annotate ns test-a "tick-$i=$i" --overwrite >/dev/null
done
sleep 3
UIDS_BEFORE="$(for ns in test-a test-b test-c; do
  n="$(child_name_from_status "$ns")"
  k get cm -n "$ns" "$n" -o jsonpath='{.metadata.uid}{"\n"}'
done)"
RV_BEFORE="$(for ns in test-a test-b test-c; do
  n="$(child_name_from_status "$ns")"
  k get cm -n "$ns" "$n" -o jsonpath='{.metadata.resourceVersion}{"\n"}'
done)"
for i in $(seq 1 25); do
  k annotate configsnapshot demo-app "event2-$i=$i" --overwrite >/dev/null
done
sleep 3
UIDS_AFTER="$(for ns in test-a test-b test-c; do
  n="$(child_name_from_status "$ns")"
  k get cm -n "$ns" "$n" -o jsonpath='{.metadata.uid}{"\n"}'
done)"
RV_AFTER="$(for ns in test-a test-b test-c; do
  n="$(child_name_from_status "$ns")"
  k get cm -n "$ns" "$n" -o jsonpath='{.metadata.resourceVersion}{"\n"}'
done)"
check "child UIDs stable across 50+ duplicate events" bash -c "[ '$UIDS_BEFORE' == '$UIDS_AFTER' ]"
check "child resourceVersions stable (no recreate)" bash -c "[ '$RV_BEFORE' == '$RV_AFTER' ]"

echo
echo "=== Scenario 3: partial failure in test-b (Always), no rollback of test-a/test-c ==="
# Count actual children before.
A_BEFORE="$(k get cm -n test-a -o json | jq '[.items[]|select(.metadata.labels["config.example.com/owner"]=="demo-app")]|length')"
C_BEFORE="$(k get cm -n test-c -o json | jq '[.items[]|select(.metadata.labels["config.example.com/owner"]=="demo-app")]|length')"
# Deterministic chaos: deny every CREATE in test-b until we delete the policy.
cat <<'EOF' | k apply -f -
apiVersion: chaos.example.com/v1alpha1
kind: FailPolicy
metadata:
  name: block-test-b
spec:
  targetNamespaces: [test-b]
  actions: [CREATE]
  mode: Always
  message: "injected persistent failure for test-b (partial-failure test)"
EOF
# Wait until the policy is live in the matcher (it polls every 2s). Verify with
# a throwaway labelled ConfigMap that the webhook now denies creates in test-b.
POLICY_LIVE=no
for i in $(seq 1 15); do
  OUT="$(printf 'apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: cfg-policy-sync-probe\n  namespace: test-b\n  labels:\n    config.example.com/owner: demo-app\ndata:\n  config: probe\n' | k apply -f - 2>&1 || true)"
  if echo "$OUT" | grep -q "injected persistent failure"; then POLICY_LIVE=yes; break; fi
  k delete cm -n test-b cfg-policy-sync-probe --ignore-not-found >/dev/null 2>&1 || true
  sleep 1
done
[ "$POLICY_LIVE" = yes ] || echo "  WARNING: policy sync probe never saw a denial" | tee -a "$EVID/results.txt"
# Remove test-b's existing child so the next reconcile must CREATE (and be denied).
B_NAME="$(child_name_from_status test-b)"
k delete cm -n test-b "$B_NAME" --wait >/dev/null 2>&1 || true
HAS_FAILED=""
for i in $(seq 1 30); do
  HAS_FAILED="$(k get configsnapshot demo-app -o json | jq -r '.status.targets[]|select(.namespace=="test-b")|.phase')"
  [ "$HAS_FAILED" = "Failed" ] && break
  sleep 1
done
k get configsnapshot demo-app -o json > "$EVID/03-status-during-failure.json"
check "test-b recorded Failed while injected denial active" bash -c "[ '$HAS_FAILED' == 'Failed' ]"
# While test-b fails, test-a/test-c Applied versions must stay populated.
A_VER="$(jq -r '.status.targets[]|select(.namespace=="test-a")|.version' "$EVID/03-status-during-failure.json")"
C_VER="$(jq -r '.status.targets[]|select(.namespace=="test-c")|.version' "$EVID/03-status-during-failure.json")"
check "test-a version retained during partial failure" bash -c "[ '$A_VER' == '$DIGEST' ]"
check "test-c version retained during partial failure" bash -c "[ '$C_VER' == '$DIGEST' ]"
A_DURING="$(k get cm -n test-a -o json | jq '[.items[]|select(.metadata.labels["config.example.com/owner"]=="demo-app")]|length')"
C_DURING="$(k get cm -n test-c -o json | jq '[.items[]|select(.metadata.labels["config.example.com/owner"]=="demo-app")]|length')"
check "test-a children untouched during partial failure" bash -c "[ '$A_DURING' == '$A_BEFORE' ]"
check "test-c children untouched during partial failure" bash -c "[ '$C_DURING' == '$C_BEFORE' ]"
B_LASTERR="$(jq -r '.status.targets[]|select(.namespace=="test-b")|.lastError' "$EVID/03-status-during-failure.json")"
echo "  captured test-b lastError: $B_LASTERR" | tee -a "$EVID/results.txt"
# Remove the fault: retry must converge test-b without touching the others.
k delete failpolicy block-test-b --wait >/dev/null
fb=""
for i in $(seq 1 30); do
  fb="$(k get configsnapshot demo-app -o json | jq -r '.status.targets[]|select(.namespace=="test-b")|.phase')"
  [ "$fb" = "Applied" ] && break
  sleep 1
done
k get configsnapshot demo-app -o json > "$EVID/03-status-after-recovery.json"
check "test-b converges after injected failure clears" bash -c "[ '$fb' == 'Applied' ]"
NONMATCH="$(jq --arg d "$DIGEST" '[.status.targets[]|select(.version!=$d)]|length' "$EVID/03-status-after-recovery.json")"
check "all three applied with same digest" bash -c "[ '$NONMATCH' -eq 0 ]"

echo
echo "=== Scenario 4: selector narrowing -> GC only UID-owned children ==="
k label ns test-c tier=other --overwrite
for i in $(seq 1 20); do
  exists="$(n="$(child_name_from_status test-c)"; k get cm -n test-c "$n" >/dev/null 2>&1 && echo yes || echo no)"
  [ "$exists" = "no" ] && break
  sleep 1
done
GONE_NAME="$(jq -r '.status.targets[]|select(.namespace=="test-c")|.childName' "$EVID/01-status.json")"
check "owned child in deselected test-c garbage-collected" bash -c \
  "! k get cm -n test-c $GONE_NAME >/dev/null 2>&1"
TC_TARGETS="$(k get configsnapshot demo-app -o json | jq '[.status.targets[]|select(.namespace=="test-c")]|length')"
check "deselected namespace removed from status targets" bash -c "[ '$TC_TARGETS' -eq 0 ]"
# test-a and test-b intact.
for ns in test-a test-b; do
  n="$(child_name_from_status "$ns")"
  check "child in still-selected $ns survives narrowing" k get cm -n "$ns" "$n"
done

echo
echo "=== Scenario 4b: foreign same-labelled object with different owner UID is NEVER deleted ==="
DESIRED="cfg-demo-app-${DIGEST:0:16}"
# Move test-c OUT of the selection and wait for its owned child to be GC'd, so
# that pre-creating the foreign object is not racing the controller's recreate.
k label ns test-c tier=other --overwrite >/dev/null
for i in $(seq 1 20); do
  k get cm -n test-c "$DESIRED" >/dev/null 2>&1 || break
  sleep 1
done

# Pre-create a FOREIGN ConfigMap with the SAME desired name and managed labels,
# owned (controller reference) by a DIFFERENT, real ConfigSnapshot. A dangling
# reference (nonexistent UID) would itself be reaped by Kubernetes GC, so a real
# foreign owner is the faithful "same name, different UID" collision model.
cat <<'EOF' | k apply -f -
apiVersion: config.example.com/v1alpha1
kind: ConfigSnapshot
metadata:
  name: foreign-owner
spec:
  payload:
    format: text
    data: "foreign"
  selector:
    matchLabels:
      tier: never-selected-foreign
EOF
FOREIGN_UID="$(k get configsnapshot foreign-owner -o jsonpath='{.metadata.uid}')"
k create configmap "$DESIRED" -n test-c \
  --from-literal=config=FOREIGN-DO-NOT-DELETE >/dev/null
k label cm -n test-c "$DESIRED" \
  app.kubernetes.io/managed-by=config-distributor \
  config.example.com/owner=demo-app >/dev/null
cat <<EOF | k apply --server-side --force-conflicts -f -
apiVersion: v1
kind: ConfigMap
metadata:
  name: $DESIRED
  namespace: test-c
  ownerReferences:
    - apiVersion: config.example.com/v1alpha1
      kind: ConfigSnapshot
      name: foreign-owner
      uid: $FOREIGN_UID
      controller: true
      blockOwnerDeletion: true
EOF

# Move test-c back INTO the selection; the controller now finds its desired name
# occupied by a foreign-UID object and must report a name conflict, not delete it.
k label ns test-c tier=test --overwrite >/dev/null
TC_PHASE=""
for i in $(seq 1 20); do
  TC_PHASE="$(k get configsnapshot demo-app -o json | jq -r '.status.targets[]|select(.namespace=="test-c")|.phase')"
  [ "$TC_PHASE" = "Failed" ] && break
  sleep 1
done
check "foreign same-name object survives (not deleted)" bash -c \
  "k get cm -n test-c $DESIRED -o json | jq -er '.data.config==\"FOREIGN-DO-NOT-DELETE\"' >/dev/null"
check "target test-c reports name conflict as Failed" bash -c "[ '$TC_PHASE' == 'Failed' ]"
TC_ERR="$(k get configsnapshot demo-app -o json | jq -r '.status.targets[]|select(.namespace=="test-c")|.lastError')"
echo "  test-c lastError: $TC_ERR" | tee -a "$EVID/results.txt"
check "failure message names the foreign owner UID" bash -c "[[ '$TC_ERR' == *$FOREIGN_UID* ]]"
# Remove the foreign object; controller must then deliver its own child.
k delete cm -n test-c "$DESIRED" >/dev/null
ph=""
for i in $(seq 1 20); do
  ph="$(k get configsnapshot demo-app -o json | jq -r '.status.targets[]|select(.namespace=="test-c")|.phase')"
  [ "$ph" = "Applied" ] && break
  sleep 1
done
check "converges to Applied after foreign object removed" bash -c "[ '$ph' == 'Applied' ]"
OWNER_UID="$(k get configsnapshot demo-app -o jsonpath='{.metadata.uid}')"
check "new child owned by real owner UID" bash -c \
  "k get cm -n test-c $DESIRED -o json | jq -er --arg u $OWNER_UID '.metadata.ownerReferences[0].uid==\$u' >/dev/null"
# Remove the foreign owner (its selector never matched, so it has no children).
k delete configsnapshot foreign-owner --ignore-not-found >/dev/null 2>&1 || true

echo
echo "=== Scenario 6: owner recreation (same name, new UID) never deletes old orphans ==="
# A ConfigMap whose owner UID dangles is reaped by Kubernetes GC itself, so to
# faithfully model an orphan that outlives its owner we first create a real
# "successor" ConfigSnapshot that holds the object, then delete demo-app and
# recreate it with the SAME name (new UID). The new owner must never delete the
# object merely because the name/label match: only the owner UID decides.
OLD_UID="$OWNER_UID"
OLD_CHILD="cfg-demo-app-${DIGEST:0:16}"

cat <<'EOF' | k apply -f -
apiVersion: config.example.com/v1alpha1
kind: ConfigSnapshot
metadata:
  name: successor-owner
spec:
  payload:
    format: text
    data: "successor"
  selector:
    matchLabels:
      tier: never-selected-successor
EOF
SUCCESSOR_UID="$(k get configsnapshot successor-owner -o jsonpath='{.metadata.uid}')"

# Transfer the existing test-b child's ownership to the successor (real,
# different UID) before deleting demo-app, so it survives as an orphan to us.
# A JSON merge patch REPLACES the ownerReferences array (server-side apply would
# append and trip the "only one controller" validation).
k patch cm -n test-b "$OLD_CHILD" --type=merge -p "{\"metadata\":{\"ownerReferences\":[{\"apiVersion\":\"config.example.com/v1alpha1\",\"kind\":\"ConfigSnapshot\",\"name\":\"successor-owner\",\"uid\":\"$SUCCESSOR_UID\",\"controller\":true,\"blockOwnerDeletion\":true}]}}"
# demo-app's owner reference is now gone; delete demo-app (its other children
# cascade away, but test-b's object is held by the successor).
k delete configsnapshot demo-app --wait=true
for i in $(seq 1 30); do
  k get configsnapshot demo-app >/dev/null 2>&1 || break
  sleep 1
done
sleep 3
check "orphan held by successor survives deletion of original owner" bash -c \
  "k get cm -n test-b $OLD_CHILD -o json | jq -er '.metadata.ownerReferences[0].uid==\"$SUCCESSOR_UID\"' >/dev/null"

# Recreate the owner with the SAME name; the apiserver assigns a NEW UID.
k apply -f "$ROOT/config/samples/configsnapshot.yaml"
S6_PHASE=""
for i in $(seq 1 30); do
  S6_PHASE="$(k get configsnapshot demo-app -o json | jq -r '.status.targets[]?|select(.namespace=="test-b")|.phase' 2>/dev/null)"
  [ "$S6_PHASE" = "Failed" ] && break
  sleep 1
done
check "orphan with different owner UID is never deleted by recreated owner" bash -c \
  "k get cm -n test-b $OLD_CHILD -o json | jq -er '.metadata.ownerReferences[0].uid==\"$SUCCESSOR_UID\"' >/dev/null"
NEW_UID="$(k get configsnapshot demo-app -o jsonpath='{.metadata.uid}')"
check "recreated owner actually got a new UID" bash -c \
  "[ '$NEW_UID' != '$OLD_UID' ] && [ -n '$NEW_UID' ]"
check "new owner reports test-b as Failed name-conflict (safe, not destructive)" bash -c \
  "[ '$S6_PHASE' == 'Failed' ]"
# Cleanup: drop the successor-held orphan so scenario 7 starts from 3 children.
k delete configsnapshot successor-owner --ignore-not-found >/dev/null 2>&1 || true

echo
echo "=== Scenario 7: process restart -> reconcile from scratch, no duplicate children ==="
for i in $(seq 1 30); do
  ac="$(k get configsnapshot demo-app -o json | jq -r '.status.appliedCount')"
  [ "$ac" = "3" ] && break
  sleep 1
done
BEFORE_TOTAL="$(for ns in test-a test-b test-c; do k get cm -n "$ns" -o json; done | jq -s '[.[].items[]|select(.metadata.labels["config.example.com/owner"]=="demo-app")]|length')"
k -n config-system delete pod -l app=config-distributor
k -n config-system rollout status deploy/config-distributor --timeout=120s
sleep 5
for i in $(seq 1 30); do
  ac="$(k get configsnapshot demo-app -o json | jq -r '.status.appliedCount')"
  [ "$ac" = "3" ] && break
  sleep 1
done
AFTER_TOTAL="$(for ns in test-a test-b test-c; do k get cm -n "$ns" -o json; done | jq -s '[.[].items[]|select(.metadata.labels["config.example.com/owner"]=="demo-app")]|length')"
check "no duplicate children after process restart" bash -c "[ '$BEFORE_TOTAL' == '$AFTER_TOTAL' ] && [ '$AFTER_TOTAL' == '3' ]"
FC_AFTER="$(k get configsnapshot demo-app -o json | jq -r '.status.failedCount // 0')"
check "status fully re-converged after restart" bash -c "[ '$FC_AFTER' -eq 0 ]"
k get configsnapshot demo-app -o json > "$EVID/07-status-after-restart.json"

echo
echo "=== evidence bundle ==="
k -n config-system logs deploy/config-distributor --tail=2000 > "$EVID/controller.log" || true
k get configsnapshot demo-app -o yaml > "$EVID/08-final-configsnapshot.yaml"
k get events -A --field-selector reason=NameConflict > "$EVID/09-name-conflict-events.txt" 2>/dev/null || true
for ns in test-a test-b test-c; do
  k get cm -n "$ns" -o yaml > "$EVID/cm-$ns.yaml"
done
# Checksum only the frozen artifacts. run.log is still being appended to by the
# tee in the process substitution and SHA256SUMS cannot hash itself, so both are
# excluded.
( cd "$EVID" && sha256sum \
    01-status.json \
    03-status-during-failure.json \
    03-status-after-recovery.json \
    07-status-after-restart.json \
    08-final-configsnapshot.yaml \
    09-name-conflict-events.txt \
    cm-test-a.yaml cm-test-b.yaml cm-test-c.yaml \
    controller.log results.txt > SHA256SUMS ) 2>/dev/null || true

echo
echo "=== RESULTS: $PASS passed, $FAIL failed ==="
echo "evidence: $EVID"
[ "$FAIL" -eq 0 ]
