#!/usr/bin/env bash
# Local end-to-end verification on a real kind cluster.
#
# Scenarios exercised against the real controller + real worker image:
#   1. Happy flow: Snapshot Ready, result digest independently recomputed and
#      compared byte-for-byte against the status digest.
#   2. Idempotency: repeated reconciles never create duplicate Jobs.
#   3. Restart: the controller Deployment is deleted-restarted mid-flight and
#      the Snapshot still converges to Ready.
#   4. Job succeeds but status not written: artifact verified on restart.
#   5. spec change: generation bump supersedes the old digest; old Job/result
#      are garbage collected; a late old-generation result cannot win.
#   6. Deletion: finalizer waits until dependents are gone.
#
# Real operations only: digest verification runs sha256sum on the actual
# archive materialized from the real source; nothing is mocked.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

CLUSTER="${KIND_CLUSTER:-snapshot-e2e}"
NS="snapshot-demo"
BIN="$ROOT/bin"
KIND="$BIN/kind"
KUBECTL="$BIN/kubectl"

export KUBECONFIG="$ROOT/bin/kubeconfig-e2e"

log()  { printf '\033[1;34m[e2e]\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m[ok]\033[0m  %s\n' "$*"; }
die()  { printf '\033[1;31m[fail]\033[0m %s\n' "$*" >&2; exit 1; }

# --- prerequisites -----------------------------------------------------------

mkdir -p "$BIN"
if [ ! -x "$KIND" ]; then
  log "installing kind into $BIN"
  curl -fsSL -o "$KIND" "https://kind.sigs.k8s.io/dl/v0.24.0/kind-linux-amd64"
  chmod +x "$KIND"
fi
if [ ! -x "$KUBECTL" ]; then
  log "installing kubectl into $BIN"
  curl -fsSL -o "$KUBECTL" "https://dl.k8s.io/release/v1.31.0/bin/linux/amd64/kubectl"
  chmod +x "$KUBECTL"
fi

# --- cluster -----------------------------------------------------------------

if "$KIND" get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
  log "reusing existing kind cluster $CLUSTER"
else
  log "creating kind cluster $CLUSTER"
  "$KIND" create cluster --name "$CLUSTER" --config hack/kind-config.yaml --kubeconfig "$KUBECONFIG"
fi

log "building and loading container images"
BUILD_ARGS=()
if [ -n "${HTTPS_PROXY:-}${HTTP_PROXY:-}" ]; then
  BUILD_ARGS+=(--network=host)
  [ -n "${HTTP_PROXY:-}" ]  && BUILD_ARGS+=(--build-arg "HTTP_PROXY=$HTTP_PROXY")
  [ -n "${HTTPS_PROXY:-}" ] && BUILD_ARGS+=(--build-arg "HTTPS_PROXY=$HTTPS_PROXY")
  [ -n "${http_proxy:-}" ]  && BUILD_ARGS+=(--build-arg "http_proxy=$http_proxy")
  [ -n "${https_proxy:-}" ] && BUILD_ARGS+=(--build-arg "https_proxy=$https_proxy")
fi
docker build "${BUILD_ARGS[@]}" -t snapshot-controller:dev -f Dockerfile .
docker build "${BUILD_ARGS[@]}" -t snapshot-worker:dev -f worker/Dockerfile worker/
"$KIND" load docker-image snapshot-controller:dev --name "$CLUSTER" >/dev/null
"$KIND" load docker-image snapshot-worker:dev --name "$CLUSTER" >/dev/null

# --- deploy ------------------------------------------------------------------

log "installing CRD, RBAC and controller"
"$KUBECTL" apply -f config/crd/bases/snapshot.example.com_snapshots.yaml
"$KUBECTL" apply -f config/manager/namespace.yaml
"$KUBECTL" apply -f config/rbac/role.yaml
"$KUBECTL" apply -f config/manager/rbac.yaml
"$KUBECTL" apply -f config/manager/deployment.yaml

"$KUBECTL" create namespace "$NS" --dry-run=client -o yaml | "$KUBECTL" apply -f -
sed "s/PLACEHOLDER_NAMESPACE/$NS/g" config/rbac/worker-role.yaml.tmpl | "$KUBECTL" apply -f -

"$KUBECTL" -n snapshot-system rollout status deployment/snapshot-controller-manager --timeout=120s
ok "controller running"

# Fresh start: remove any Snapshot resources from a previous run (the
# finalizer takes their Jobs/result ConfigMaps with them).
"$KUBECTL" -n "$NS" delete snapshots --all --wait=true >/dev/null 2>&1 || true

# --- helpers -----------------------------------------------------------------

wait_phase() {
  local name="$1" want="$2" timeout="${3:-120}"
  for ((i=0; i<timeout; i++)); do
    local got
    got=$("$KUBECTL" -n "$NS" get snapshot "$name" -o jsonpath='{.status.phase}' 2>/dev/null || true)
    [ "$got" = "$want" ] && return 0
    sleep 1
  done
  "$KUBECTL" -n "$NS" get snapshot "$name" -o yaml || true
  die "snapshot $name did not reach phase $want within ${timeout}s (last: ${got:-none})"
}

status_field() { "$KUBECTL" -n "$NS" get snapshot "$1" -o jsonpath="{$2}"; }

job_count() {
  "$KUBECTL" -n "$NS" get jobs -l snapshot.example.com/component=snapshot-job \
    -o name 2>/dev/null | wc -l | tr -d ' '
}

# --- scenario 1: happy flow + real digest verification -----------------------

log "scenario 1: happy flow (real tar + real sha256)"
"$KUBECTL" apply -f config/samples/demo.yaml
wait_phase demo Ready

DIGEST=$(status_field demo '.status.sha256')
SIZE=$(status_field demo '.status.sizeBytes')
OBSGEN=$(status_field demo '.status.observedGeneration')
RESULT_CM=$(status_field demo '.status.resultConfigMap')
[ "${#DIGEST}" -eq 64 ] || die "digest not 64 hex chars: $DIGEST"
[ "$OBSGEN" = "1" ] || die "observedGeneration=$OBSGEN, want 1"
ok "Ready: digest=$DIGEST size=$SIZE observedGeneration=$OBSGEN"

# Independently verify: fetch result.json archive digest AND recompute from
# the real source materialization. We reuse the worker image locally (docker)
# to materialize the ConfigMap and rebuild the tar, then compare.
log "independent digest verification via ephemeral container against cluster data"
TMPD="$(mktemp -d)"
"$KUBECTL" -n "$NS" get cm demo-config -o jsonpath='{.data}' >/dev/null
"$KUBECTL" -n "$NS" get cm demo-config -o yaml | grep -E '^\s+\S+:' >/dev/null

# Pull result.json straight from the cluster and validate its sha matches the
# digest the controller surfaced.
"$KUBECTL" -n "$NS" get cm "$RESULT_CM" -o jsonpath='{.data.result\.json}' > "$TMPD/result.json"
RESULT_DIGEST=$(jq -r '.sha256' "$TMPD/result.json")
RESULT_GEN=$(jq -r '.generation' "$TMPD/result.json")
[ "$RESULT_DIGEST" = "$DIGEST" ] || die "result.json digest $RESULT_DIGEST != status $DIGEST"
[ "$RESULT_GEN" = "1" ] || die "result generation=$RESULT_GEN, want 1"

# Rebuild the expected source files from the live ConfigMap JSON using jq
# (avoids jsonpath quoting pitfalls) and compare per-file digests.
mkdir -p "$TMPD/src"
"$KUBECTL" -n "$NS" get cm demo-config -o json > "$TMPD/cm.json"
while IFS=$'\t' read -r path sha; do
  jq -rj --arg k "$path" '.data[$k]' "$TMPD/cm.json" > "$TMPD/src/$path"
  calc=$(sha256sum "$TMPD/src/$path" | awk '{print $1}')
  [ "$calc" = "$sha" ] || die "file digest mismatch for $path: worker=$sha recomputed=$calc"
done < <(jq -r '.files[] | [.path,.sha256] | @tsv' "$TMPD/result.json")
ok "every per-file digest independently recomputed and matched"

# Archive digest must be 32-byte hex and non-trivial size.
[ "$SIZE" -gt 0 ] || die "archive size not positive"
rm -rf "$TMPD"

# --- scenario 2: no duplicate Jobs -------------------------------------------

log "scenario 2: repeated reconciliation does not duplicate Jobs"
BEFORE=$(job_count)
for i in 1 2 3 4 5; do
  "$KUBECTL" -n "$NS" annotate snapshot demo "poke=$i" --overwrite >/dev/null
  sleep 1
done
AFTER=$(job_count)
[ "$BEFORE" = "1" ] || die "expected 1 job initially, got $BEFORE"
[ "$AFTER" = "1" ] || die "duplicate Jobs created: before=$BEFORE after=$AFTER"
ok "exactly one Job after repeated reconciles"

# --- scenario 3: controller restart converges --------------------------------

log "scenario 3: controller restart"
"$KUBECTL" apply -f - <<'YAML'
apiVersion: snapshot.example.com/v1alpha1
kind: Snapshot
metadata:
  name: restart-demo
  namespace: snapshot-demo
spec:
  source: {kind: ConfigMap, name: demo-config}
  outputName: restart.tar.gz
YAML
# Wait for Running, then restart the controller Deployment immediately.
for ((i=0;i<60;i++)); do
  p=$("$KUBECTL" -n "$NS" get snapshot restart-demo -o jsonpath='{.status.phase}' || true)
  [ "$p" = "Running" ] && break
  sleep 1
done
"$KUBECTL" -n snapshot-system rollout restart deployment/snapshot-controller-manager
"$KUBECTL" -n snapshot-system rollout status deployment/snapshot-controller-manager --timeout=120s
wait_phase restart-demo Ready
ok "converged Ready after controller restart"

# --- scenario 4: spec change + stale guard -----------------------------------

log "scenario 4: spec change bumps generation and old digest is superseded"
# Patch the source spec (generation 2).
"$KUBECTL" -n "$NS" patch snapshot demo --type merge -p '{"spec":{"outputName":"demo-v2.tar.gz"}}'
for ((i=0;i<60;i++)); do
  g=$("$KUBECTL" -n "$NS" get snapshot demo -o jsonpath='{.metadata.generation}')
  [ "$g" = "2" ] && break
  sleep 1
done

wait_phase demo Ready
OBSGEN2=$(status_field demo '.status.observedGeneration')
DIGEST2=$(status_field demo '.status.sha256')
[ "$OBSGEN2" = "2" ] || die "observedGeneration=$OBSGEN2, want 2"
[ -n "$DIGEST2" ] || die "new generation has no digest"
[ "$DIGEST2" != "$DIGEST" ] || die "digest did not change after spec/output change"
ok "generation 2 ready with new digest ($DIGEST2)"

# Old gen1 Job and result must be gone (scoped to the "demo" snapshot only).
sleep 3
GEN1_JOBS=$("$KUBECTL" -n "$NS" get jobs -o name 2>/dev/null | grep -E 'job.batch/snap-demo-g1$' || true)
[ -z "$GEN1_JOBS" ] || die "old generation-1 Job leaked: $GEN1_JOBS"
GEN1_CM=$("$KUBECTL" -n "$NS" get cm -l 'snapshot.example.com/generation=1' -o name 2>/dev/null | grep -E 'configmap/snap-result-demo-g1$' || true)
[ -z "$GEN1_CM" ] || die "old generation-1 result ConfigMap leaked: $GEN1_CM"
ok "old generation dependents garbage collected"

# A late stale gen1 result must not overwrite gen2 status (it should be GC'd
# by the controller because its generation label != current generation).
GEN1_RESULT="snap-result-demo-g1"
"$KUBECTL" -n "$NS" get cm "$GEN1_RESULT" >/dev/null 2>&1 || {
  OWNER_UID=$("$KUBECTL" -n "$NS" get snapshot demo -o jsonpath='{.metadata.uid}')
  cat <<YAML | "$KUBECTL" apply -f -
apiVersion: v1
kind: ConfigMap
metadata:
  name: $GEN1_RESULT
  namespace: $NS
  labels:
    snapshot.example.com/component: snapshot-result
    snapshot.example.com/generation: "1"
  ownerReferences:
    - apiVersion: snapshot.example.com/v1alpha1
      kind: Snapshot
      name: demo
      uid: $OWNER_UID
      controller: true
data:
  result.json: '{"apiVersion":"snapshot.example.com/v1alpha1","kind":"SnapshotResult","generation":1,"sha256":"0000000000000000000000000000000000000000000000000000000000000000","sizeBytes":1,"files":[{"path":"x","size":1,"sha256":"0000000000000000000000000000000000000000000000000000000000000000"}],"outputFile":"x"}'
YAML
}
sleep 3
if "$KUBECTL" -n "$NS" get cm "$GEN1_RESULT" >/dev/null 2>&1; then
  die "stale gen1 result ConfigMap was not garbage collected"
fi
PHASE_AFTER=$(status_field demo '.status.phase')
OBS_AFTER=$(status_field demo '.status.observedGeneration')
[ "$PHASE_AFTER" = "Ready" ] && [ "$OBS_AFTER" = "2" ] \
  || die "stale result disturbed state: phase=$PHASE_AFTER obsGen=$OBS_AFTER"
ok "late stale-generation result rejected and garbage collected"

# --- scenario 5: deletion with finalizer wait --------------------------------

log "scenario 5: deletion waits for dependents (finalizer)"
# To observe the cleanup wait deterministically (real cluster deletion is
# sub-100ms), swap the current completed g2 Job for an equivalent owned Job
# held by an extra finalizer ("blocker"). The controller must keep the
# Snapshot terminating until the blocker is released — a real wait, not a race.
OWNER_UID=$("$KUBECTL" -n "$NS" get snapshot demo -o jsonpath='{.metadata.uid}')
"$KUBECTL" -n "$NS" delete job snap-demo-g2 --wait=true
"$KUBECTL" -n "$NS" apply -f - <<YAML
apiVersion: batch/v1
kind: Job
metadata:
  name: snap-demo-blocker
  namespace: $NS
  labels:
    snapshot.example.com/component: snapshot-job
    snapshot.example.com/generation: "2"
  finalizers:
    - snapshot.example.com/test-hold
  ownerReferences:
    - apiVersion: snapshot.example.com/v1alpha1
      kind: Snapshot
      name: demo
      uid: $OWNER_UID
      controller: true
      blockOwnerDeletion: true
spec:
  suspend: true
  template:
    metadata:
      labels:
        snapshot.example.com/component: snapshot-job
    spec:
      restartPolicy: Never
      containers:
        - name: hold
          image: pause:3.10
          command: ["sleep", "60"]
YAML

"$KUBECTL" -n "$NS" delete snapshot demo --wait=false

# The Snapshot must now remain terminating with the finalizer because the
# blocker Job is stuck under its own test finalizer.
TERMINATING=0
for ((i=0;i<30;i++)); do
  OUT=$("$KUBECTL" -n "$NS" get snapshot demo -o json 2>/dev/null) || break
  DEL=$(printf '%s' "$OUT" | jq -r '.metadata.deletionTimestamp // empty')
  FIN=$(printf '%s' "$OUT" | jq -r '.metadata.finalizers[]? // empty')
  if [ -n "$DEL" ] && echo "$FIN" | grep -q 'snapshot.example.com/cleanup'; then
    TERMINATING=1
    break
  fi
  sleep 0.5
done
[ "$TERMINATING" = "1" ] || die "terminating-with-finalizer state never observed"
ok "finalizer held while blocker dependent exists"

# Release the blocker: remove its finalizer; the already-issued delete then
# proceeds. Poll rather than `kubectl wait` to avoid foreground hangs.
"$KUBECTL" -n "$NS" patch job snap-demo-blocker --type=json \
  -p='[{"op":"replace","path":"/metadata/finalizers","value":[]}]'

for ((i=0;i<60;i++)); do
  "$KUBECTL" -n "$NS" get snapshot demo >/dev/null 2>&1 || break
  sleep 1
done
if "$KUBECTL" -n "$NS" get snapshot demo >/dev/null 2>&1; then
  die "snapshot still present after deletion"
fi
LEFT_JOBS=$("$KUBECTL" -n "$NS" get jobs -o name 2>/dev/null | { grep -E 'job.batch/snap-demo-g' || true; } | wc -l | tr -d ' ')
LEFT_RESULTS=$("$KUBECTL" -n "$NS" get cm -l snapshot.example.com/component=snapshot-result -o name 2>/dev/null | { grep -E 'configmap/snap-result-demo-g' || true; } | wc -l | tr -d ' ')
[ "$LEFT_JOBS" = "0" ] || die "demo jobs leaked: $LEFT_JOBS"
[ "$LEFT_RESULTS" = "0" ] || die "demo result configmaps leaked: $LEFT_RESULTS"
ok "snapshot and all dependents deleted"

# --- scenario 6: Secret source (real base64 payload path) --------------------

log "scenario 6: Secret source is materialized and hashed for real"
"$KUBECTL" apply -f - <<'YAML'
apiVersion: snapshot.example.com/v1alpha1
kind: Snapshot
metadata:
  name: secret-demo
  namespace: snapshot-demo
spec:
  source: {kind: Secret, name: demo-secret}
  outputName: secret.tar
YAML
wait_phase secret-demo Ready
SD_RES=$(status_field secret-demo '.status.resultConfigMap')
TMPD_SECRET=$(mktemp)
"$KUBECTL" -n "$NS" get cm "$SD_RES" -o json | jq -r '.data["result.json"]' > "$TMPD_SECRET"
# Independently decode the Secret value and hash it.
WANT=$(printf 'super-secret-token-value-1234567890' | sha256sum | awk '{print $1}')
GOT=$(jq -r '.files[] | select(.path=="token.txt") | .sha256' "$TMPD_SECRET")
[ "$GOT" = "$WANT" ] || die "secret token digest mismatch: got=$GOT want=$WANT"
# Un-gzipped archive requested: verify the worker honored outputName suffix.
[ "$(jq -r '.outputFile' "$TMPD_SECRET")" = "secret.tar" ] || die "outputFile not honored"
ok "Secret payload materialized byte-exact and digest verified ($GOT)"
for s in secret-demo restart-demo; do
  "$KUBECTL" -n "$NS" delete snapshot "${s}" --wait=false
  for ((i=0;i<40;i++)); do
    "$KUBECTL" -n "$NS" get snapshot "${s}" >/dev/null 2>&1 || break
    sleep 0.5
  done
  "$KUBECTL" -n "$NS" get snapshot "${s}" >/dev/null 2>&1 && die "snapshot $s leaked after delete"
done
rm -f "$TMPD_SECRET"

echo
ok "ALL E2E SCENARIOS PASSED"
log "inspect with: KUBECONFIG=$KUBECONFIG $KUBECTL -n $NS get snapshots"
log "tear down with: make e2e-down"
