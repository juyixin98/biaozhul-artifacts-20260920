#!/usr/bin/env bash
# End-to-end demo of the ConfigDistribution controller on a fresh kind cluster.
#
# Scenarios (each prints PASS/FAIL; any failure preserves evidence under
# artifacts/ and exits non-zero):
#   1. fan-out to selected namespaces, digest-named children
#   2. duplicate events / full resync -> no duplicates, no rewrites
#   3. config immutability enforced by the API server (CEL)
#   4. out-of-band child delete -> recreated (out-of-order safety)
#   5. selector shrink -> only owned children removed; foreign same-name
#      object (different owner UID) survives; conflict reported per target
#   6. owner rebuild (CR deleted with finalizer force-removed, recreated with
#      a new UID) -> orphans adopted, never deleted
#   7. controller process restart -> pure no-op
#   8. CR delete -> owned children collected, foreign objects untouched
set -euo pipefail

CLUSTER="${CLUSTER:-p090b-e2e}"
KIND_IMAGE="${KIND_IMAGE:-kindest/node:v1.30.10}"
KEEP_CLUSTER="${KEEP_CLUSTER:-0}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ARTIFACTS="$ROOT/artifacts/e2e-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$ARTIFACTS"

K="kubectl --context kind-$CLUSTER"
CTRL_PID=""
FAILED=0

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
pass() { printf '\033[1;32mPASS\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31mFAIL\033[0m %s\n' "$*"; FAILED=1; }

collect_evidence() {
  local why="$1"
  log "collecting failure evidence ($why) into $ARTIFACTS"
  {
    echo "### $why"
    echo "### cfds";    $K get configdistributions.dist.example.com -o yaml 2>&1 || true
    echo "### configmaps"; $K get configmaps -A -o wide 2>&1 || true
    echo "### managed configmaps yaml"
    $K get configmaps -A -l app.kubernetes.io/managed-by=config-distributor -o yaml 2>&1 || true
    echo "### namespaces"; $K get ns --show-labels 2>&1 || true
    echo "### events";  $K get events -A --sort-by=.lastTimestamp 2>&1 | tail -50 || true
  } > "$ARTIFACTS/cluster-state.txt" 2>&1 || true
  [[ -f "$ARTIFACTS/controller.log" ]] && tail -200 "$ARTIFACTS/controller.log" > "$ARTIFACTS/controller-tail.log" || true
}

on_exit() {
  local rc=$?
  stop_controller
  if [[ $rc -ne 0 || $FAILED -ne 0 ]]; then
    collect_evidence "script exited rc=$rc failed=$FAILED"
    echo "E2E FAILED — evidence preserved in $ARTIFACTS" >&2
    [[ "$KEEP_CLUSTER" == "1" ]] || kind delete cluster --name "$CLUSTER" >/dev/null 2>&1 || true
    exit 1
  fi
  [[ "$KEEP_CLUSTER" == "1" ]] || kind delete cluster --name "$CLUSTER" >/dev/null 2>&1 || true
  echo "E2E OK — artifacts in $ARTIFACTS"
}
trap on_exit EXIT

start_controller() {
  log "starting controller (log: $ARTIFACTS/controller.log)"
  (cd "$ROOT" && exec "$ROOT/bin/manager-e2e" \
    -metrics-bind-address "${METRICS_ADDR:-:0}" \
    -health-probe-bind-address "${PROBE_ADDR:-:0}") \
    >>"$ARTIFACTS/controller.log" 2>&1 &
  CTRL_PID=$!
  sleep 1
}
stop_controller() {
  if [[ -n "$CTRL_PID" ]] && kill -0 "$CTRL_PID" 2>/dev/null; then
    kill "$CTRL_PID" 2>/dev/null || true
    wait "$CTRL_PID" 2>/dev/null || true
  fi
  CTRL_PID=""
}

wait_jsonpath() { # wait_jsonpath <resource> <name> <jsonpath> <expected> <timeout-s>
  local res="$1" name="$2" jp="$3" want="$4" timeout="${5:-60}" got=""
  for ((i=0; i<timeout; i++)); do
    got="$($K get "$res" "$name" -o "jsonpath=$jp" 2>/dev/null || true)"
    [[ "$got" == "$want" ]] && return 0
    sleep 1
  done
  echo "timeout waiting for $res/$name $jp == $want (last: $got)" >&2
  return 1
}

child_count() { $K get cm -A -l "dist.example.com/owner-name=main" --no-headers 2>/dev/null | wc -l | tr -d ' '; }
child_uid()   { $K -n "$1" get cm "$2" -o jsonpath='{.metadata.uid}' 2>/dev/null || true; }

# ---------------------------------------------------------------- cluster up
log "building controller binary"
(cd "$ROOT" && go build -o bin/manager-e2e ./cmd/manager)

log "creating kind cluster $CLUSTER ($KIND_IMAGE)"
kind delete cluster --name "$CLUSTER" >/dev/null 2>&1 || true
kind create cluster --name "$CLUSTER" --image "$KIND_IMAGE" --wait 120s

$K apply -f "$ROOT/config/crd/bases/dist.example.com_configdistributions.yaml"
$K apply -f "$ROOT/config/samples/namespaces.yaml"
start_controller

# ------------------------------------------------------- 1. fan-out + digest
log "scenario 1: distribute config to selected namespaces"
$K apply -f "$ROOT/config/samples/dist_v1alpha1_configdistribution.yaml"
DIGEST=""
for ((i=0; i<60; i++)); do
  DIGEST="$($K get cfds main -o jsonpath='{.status.configDigest}' 2>/dev/null || true)"
  [[ -n "$DIGEST" ]] && break
  sleep 1
done
[[ -n "$DIGEST" ]] || { fail "status.configDigest never set"; exit 1; }
CHILD="main-${DIGEST:0:12}"
echo "digest=$DIGEST child=$CHILD"
wait_jsonpath cfds main '{.status.conditions[?(@.type=="Ready")].status}' True 60 \
  && pass "Ready condition" || fail "Ready condition"
[[ "$(child_count)" == "2" ]] && pass "exactly 2 children" || fail "expected 2 children, got $(child_count)"
for ns in team-a-1 team-a-2; do
  $K -n "$ns" get cm "$CHILD" >/dev/null && pass "child in $ns" || fail "child missing in $ns"
done
$K -n team-b-1 get cm "$CHILD" >/dev/null 2>&1 && fail "child leaked into team-b-1" || pass "no child in team-b-1"
V1_A1="$(child_uid team-a-1 "$CHILD")"; V1_A2="$(child_uid team-a-2 "$CHILD")"

# ------------------------------------------- 2. duplicate events / resync
log "scenario 2: duplicate apply + resync must not duplicate or rewrite"
$K apply -f "$ROOT/config/samples/dist_v1alpha1_configdistribution.yaml"
$K annotate cfds main "test.example.com/resync=$(date +%s)" --overwrite >/dev/null
sleep 4
[[ "$(child_count)" == "2" ]] && pass "still 2 children after duplicate apply" || fail "duplicates: $(child_count)"
[[ "$(child_uid team-a-1 "$CHILD")" == "$V1_A1" && "$(child_uid team-a-2 "$CHILD")" == "$V1_A2" ]] \
  && pass "child UIDs stable (no recreate/rewrite)" || fail "children recreated on resync"

# ------------------------------------------------------- 3. immutability
log "scenario 3: spec.config is immutable"
if $K patch cfds main --type=merge -p '{"spec":{"config":{"app.conf":"mode=slow"}}}' 2>"$ARTIFACTS/immutable-patch.txt"; then
  fail "API server accepted config mutation"
else
  grep -q "immutable" "$ARTIFACTS/immutable-patch.txt" && pass "mutation rejected: $(tail -1 "$ARTIFACTS/immutable-patch.txt")" \
    || fail "mutation rejected but unexpected message"
fi

# ------------------------------------------- 4. out-of-band delete (order)
log "scenario 4: out-of-band child delete is healed"
$K -n team-a-1 delete cm "$CHILD" >/dev/null
for ((i=0; i<30; i++)); do [[ -n "$(child_uid team-a-1 "$CHILD")" ]] && break; sleep 1; done
[[ -n "$(child_uid team-a-1 "$CHILD")" ]] && pass "child recreated in team-a-1" || fail "child not recreated"

# ----------------- 5. selector shrink + foreign same-name object protection
log "scenario 5: selector shrink cleans up only owned objects"
# Plant a foreign object with the SAME deterministic name in team-b-1.
$K -n team-b-1 create configmap "$CHILD" --from-literal=app.conf=mode=foreign >/dev/null
# Extend selector to cover team-b-1 -> conflict must be reported, object kept.
$K label ns team-b-1 team=a --overwrite >/dev/null
CONFLICT=""
for ((i=0; i<30; i++)); do
  CONFLICT="$($K get cfds main -o jsonpath='{.status.targets[?(@.namespace=="team-b-1")].phase}' 2>/dev/null || true)"
  [[ "$CONFLICT" == "Conflict" ]] && break
  sleep 1
done
[[ "$CONFLICT" == "Conflict" ]] && pass "conflict reported for team-b-1" || fail "no conflict status (got '$CONFLICT')"
[[ "$($K -n team-b-1 get cm "$CHILD" -o jsonpath='{.data.app\.conf}')" == "mode=foreign" ]] \
  && pass "foreign object untouched during conflict" || fail "foreign object modified"
# Successful targets stay Ready while team-b-1 conflicts.
[[ "$($K get cfds main -o jsonpath='{.status.targets[?(@.namespace=="team-a-1")].phase}')" == "Ready" ]] \
  && pass "team-a-1 stays Ready during sibling conflict" || fail "team-a-1 rolled back"
# Resolve conflict -> converges without touching successes.
$K -n team-b-1 delete cm "$CHILD" >/dev/null
wait_jsonpath cfds main '{.status.conditions[?(@.type=="Ready")].status}' True 60 \
  && pass "converged after conflict resolution" || fail "did not converge"
# Now shrink: move team-a-2 away; its owned child must be deleted.
$K label ns team-a-2 team=c --overwrite >/dev/null
for ((i=0; i<30; i++)); do [[ "$(child_count)" == "2" ]] && break; sleep 1; done
[[ "$(child_count)" == "2" ]] && pass "shrink removed team-a-2 child (2 remain: team-a-1, team-b-1)" \
  || fail "wrong child count after shrink: $(child_count)"
$K -n team-a-2 get cm "$CHILD" >/dev/null 2>&1 && fail "owned child in team-a-2 not collected" \
  || pass "owned child in team-a-2 collected"

# ------------------------------------------------------- 6. owner rebuild
log "scenario 6: owner rebuild adopts orphans (no delete/recreate)"
UID_B1_BEFORE="$(child_uid team-b-1 "$CHILD")"
UID_A1_BEFORE="$(child_uid team-a-1 "$CHILD")"
# Simulate a crashed controller: stop it FIRST, then force-remove the
# finalizer and delete the CR with orphan cascade. Otherwise the running
# controller would (correctly) re-add the finalizer and collect the children.
stop_controller
$K patch cfds main --type=merge -p '{"metadata":{"finalizers":[]}}' >/dev/null
$K delete cfds main --cascade=orphan --wait=false >/dev/null
for ((i=0; i<30; i++)); do $K get cfds main >/dev/null 2>&1 || break; sleep 1; done
$K get cfds main >/dev/null 2>&1 && fail "CR not deleted" || pass "CR force-deleted while controller down"
[[ "$(child_count)" == "2" ]] && pass "orphans remain (GC did not cascade)" || fail "orphans vanished"
# Rebuild: same name, same spec, NEW uid. Restart the controller.
$K apply -f "$ROOT/config/samples/dist_v1alpha1_configdistribution.yaml"
start_controller
wait_jsonpath cfds main '{.status.conditions[?(@.type=="Ready")].status}' True 60 \
  && pass "rebuilt CR Ready" || fail "rebuilt CR not Ready"
NEW_CR_UID="$($K get cfds main -o jsonpath='{.metadata.uid}')"
[[ "$(child_uid team-a-1 "$CHILD")" == "$UID_A1_BEFORE" && "$(child_uid team-b-1 "$CHILD")" == "$UID_B1_BEFORE" ]] \
  && pass "orphans adopted in place (UIDs preserved)" || fail "orphans were deleted+recreated"
[[ "$($K -n team-a-1 get cm "$CHILD" -o jsonpath='{.metadata.labels.dist\.example\.com/owner-uid}')" == "$NEW_CR_UID" ]] \
  && pass "owner-uid label updated to new CR UID" || fail "owner-uid label not updated"

# ------------------------------------------------------- 7. process restart
log "scenario 7: controller restart is a no-op"
stop_controller
sleep 2
start_controller
sleep 6
[[ "$(child_count)" == "2" ]] && pass "no duplicates after restart" || fail "duplicates after restart: $(child_count)"
[[ "$(child_uid team-a-1 "$CHILD")" == "$UID_A1_BEFORE" ]] \
  && pass "children untouched by restart" || fail "children rewritten by restart"

# ------------------------------------------------------- 8. delete + GC
log "scenario 8: CR delete collects owned children only"
$K -n team-b-1 create configmap foreign-keep --from-literal=x=y >/dev/null
$K delete cfds main --wait=true --timeout=60s >/dev/null
[[ "$(child_count)" == "0" ]] && pass "owned children collected" || fail "owned children remain: $(child_count)"
$K -n team-b-1 get cm foreign-keep >/dev/null && pass "foreign object preserved" || fail "foreign object deleted"

echo
if [[ $FAILED -eq 0 ]]; then
  echo "ALL SCENARIOS PASSED"
else
  echo "SOME SCENARIOS FAILED" >&2
fi
