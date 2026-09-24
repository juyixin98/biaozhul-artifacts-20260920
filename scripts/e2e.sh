#!/usr/bin/env bash
# End-to-end test against a real single-node kind cluster.
#
# It exercises the requirements with REAL resources:
#   * a real worker Job running on the node image, producing files and real
#     SHA-256 digests with coreutils sha256sum + kubectl
#   * Pending -> Running -> Ready/Failed transitions, observedGeneration binding
#   * no duplicate Jobs on repeated reconciles
#   * controller restart while a generation is in flight
#   * spec change => new generation, late old Job cannot overwrite
#   * finalizer draining Job + result ConfigMap on delete
#   * independent digest verification (cmd/verifier + a shell recompute)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"

export PATH="$HOME/.local/bin:$PATH"
KIND="${KIND:-kind}"
KUBECTL="${KUBECTL:-kubectl}"
CLUSTER="snapshot-p081a"
CTX="kind-snapshot-p081a"
NS="default"
REG="localhost:5000"
IMG="$REG/snapshot-controller:dev"
WORKER_IMG="$REG/snapshot-worker:dev"

RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; YELLOW=$'\033[0;33m'; NC=$'\033[0m'
pass() { echo "${GREEN}PASS${NC} $*"; }
info() { echo "${YELLOW}==>${NC} $*"; }
fail() { echo "${RED}FAIL${NC} $*" >&2; exit 1; }

# ---- cluster ----
info "creating kind cluster '$CLUSTER' (image kindest/node:v1.30.10)"
"$KIND" get clusters 2>/dev/null | grep -q "^${CLUSTER}$" || \
  "$KIND" create cluster --name "$CLUSTER" --config kind-cluster.yaml

info "ensuring local registry at $REG"
bash scripts/devregistry.sh "$CLUSTER"

info "building + pushing controller and worker images to local registry"
docker build -t "$IMG" .
docker push "$IMG" >/dev/null
mkdir -p buildctx
cp "$(command -v kubectl)" buildctx/kubectl
docker build -f Dockerfile.worker -t "$WORKER_IMG" buildctx
docker push "$WORKER_IMG" >/dev/null

info "applying CRD, controller, worker RBAC"
"$KUBECTL" --context "$CTX" apply -f config/crd/bases/ >/dev/null
"$KUBECTL" --context "$CTX" apply -f config/manager/manager.yaml >/dev/null
"$KUBECTL" --context "$CTX" apply -f config/rbac/worker.yaml >/dev/null
"$KUBECTL" --context "$CTX" -n snapshot-system rollout status deploy/snapshot-controller --timeout=180s

# Idempotent reset of the prior run's resources (finalizer drains dependents).
info "resetting resources from any previous run"
"$KUBECTL" --context "$CTX" -n "$NS" delete snapshots --all --wait=true >/dev/null 2>&1 || true
"$KUBECTL" --context "$CTX" -n "$NS" delete configmap e2e-src >/dev/null 2>&1 || true

wait_phase() { # <name> <phase> [timeout]
  local name="$1" want="$2" timeout="${3:-120}"
  local deadline=$((SECONDS+timeout))
  while (( SECONDS < deadline )); do
    local phase
    phase="$("$KUBECTL" --context "$CTX" -n "$NS" get snapshot "$name" -o jsonpath='{.status.phase}' 2>/dev/null || true)"
    [[ "$phase" == "$want" ]] && return 0
    sleep 1
  done
  "$KUBECTL" --context "$CTX" -n "$NS" get snapshot "$name" -o yaml || true
  fail "snapshot $name never reached $want (last phase: ${phase:-none})"
}

status_field() { "$KUBECTL" --context "$CTX" -n "$NS" get snapshot "$1" -o jsonpath="$2"; }

job_count() { "$KUBECTL" --context "$CTX" -n "$NS" get jobs -l snapshot.example.com/snapshot="$1" -o name | wc -l; }

# ---- 1. happy path with real digest ----
info "1. happy path: source ConfigMap -> real Job -> Ready"
"$KUBECTL" --context "$CTX" -n "$NS" create configmap e2e-src \
  --from-literal=alpha.txt="alpha-content" \
  --from-literal=beta.txt="beta-content-longer" \
  --dry-run=client -o yaml | "$KUBECTL" --context "$CTX" apply -f - >/dev/null
cat <<'YAML' | "$KUBECTL" --context "$CTX" apply -f - >/dev/null
apiVersion: snapshot.example.com/v1alpha1
kind: Snapshot
metadata:
  name: e2e-full
  namespace: default
spec:
  sourceConfigMap: e2e-src
YAML
wait_phase e2e-full Ready

[[ "$(status_field e2e-full '{.status.observedGeneration}')" == "1" ]] || fail "observedGeneration != 1"
[[ "$(status_field e2e-full '{.status.algorithm}')" == "SHA-256" ]] || fail "algorithm wrong"
[[ "$(status_field e2e-full '{.status.fileCount}')" == "2" ]] || fail "fileCount wrong"
digest1="$(status_field e2e-full '{.status.digest}')"
[[ ${#digest1} -eq 64 ]] || fail "digest not a sha256 hex string: $digest1"
pass "Ready with digest $digest1 (observedGeneration=1)"

# Repeated reconciles must not create more than one Job.
sleep 5
[[ "$(job_count e2e-full)" -eq 1 ]] || fail "expected exactly 1 Job, got $(job_count e2e-full)"
pass "idempotent: exactly one worker Job after repeated reconciles"

# ---- 2. independent verification of the real digest ----
info "2. independent digest verification (Go verifier + shell)"
go run ./cmd/verifier --context "$CTX" --snapshot e2e-full --namespace "$NS"

# Shell recompute: extract the source into a temp dir exactly like a mount,
# rebuild the manifest and hash it.
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
"$KUBECTL" --context "$CTX" -n "$NS" get cm e2e-src -o jsonpath='{.data}' >/dev/null
for key in alpha.txt beta.txt; do
  # go-template index (not jsonpath) correctly handles keys containing dots.
  "$KUBECTL" --context "$CTX" -n "$NS" get cm e2e-src \
    -o go-template="{{index .data \"$key\"}}" > "$tmp/$key"
done
manifest_shell="$(
  for f in $(cd "$tmp" && LC_ALL=C ls -1 | LC_ALL=C sort); do
    h="$(sha256sum "$tmp/$f" | awk '{print $1}')"
    sz="$(wc -c < "$tmp/$f" | tr -d ' ')"
    printf '%s  %s  %s\n' "$h" "$sz" "$f"
  done
)"
manifest_shell="${manifest_shell%$'\n'}"
digest_shell="$(printf '%s' "$manifest_shell" | sha256sum | awk '{print $1}')"
[[ "$digest_shell" == "$digest1" ]] || fail "shell-recomputed digest $digest_shell != recorded $digest1"
pass "shell recompute matches recorded digest"

# ---- 3. failure path (missing subPath key) ----
info "3. deterministic failure on missing subPath"
cat <<'YAML' | "$KUBECTL" --context "$CTX" apply -f - >/dev/null
apiVersion: snapshot.example.com/v1alpha1
kind: Snapshot
metadata:
  name: e2e-missing
  namespace: default
spec:
  sourceConfigMap: e2e-src
  subPath: does-not-exist.txt
YAML
wait_phase e2e-missing Failed
[[ -n "$(status_field e2e-missing '{.status.failureReason}')" ]] || fail "missing failure reason"
[[ "$(status_field e2e-missing '{.status.observedGeneration}')" == "1" ]] || fail "failure gen binding"
pass "Failed with reason $(status_field e2e-missing '{.status.failureReason}')"

# ---- 4. restart while a generation is in flight ----
info "4. controller restart during processing"
cat <<'YAML' | "$KUBECTL" --context "$CTX" apply -f - >/dev/null
apiVersion: snapshot.example.com/v1alpha1
kind: Snapshot
metadata:
  name: e2e-restart
  namespace: default
spec:
  sourceConfigMap: e2e-src
  subPath: alpha.txt
YAML
wait_phase e2e-restart Running
# Kill the controller immediately; the worker Job must continue unaided and a
# fresh controller must observe Ready from cluster state.
"$KUBECTL" --context "$CTX" -n snapshot-system delete pod -l app=snapshot-controller --wait >/dev/null
"$KUBECTL" --context "$CTX" -n snapshot-system rollout status deploy/snapshot-controller --timeout=180s
wait_phase e2e-restart Ready
[[ "$(job_count e2e-restart)" -eq 1 ]] || fail "restart created duplicate jobs: $(job_count e2e-restart)"
go run ./cmd/verifier --context "$CTX" --snapshot e2e-restart --namespace "$NS" >/dev/null
pass "recovered Ready after controller restart, single Job reused"

# ---- 5. spec change => new generation; old job cannot overwrite ----
info "5. spec change: old generation Job cannot overwrite new state"
"$KUBECTL" --context "$CTX" -n "$NS" annotate snapshot e2e-full ready-for-gen2=x >/dev/null 2>&1 || true
"$KUBECTL" --context "$CTX" -n "$NS" patch snapshot e2e-full --type=merge -p '{"spec":{"subPath":"beta.txt"}}' >/dev/null
wait_phase e2e-full Running
og="$(status_field e2e-full '{.status.observedGeneration}')"
[[ "$og" == "2" ]] || fail "expected observedGeneration=2 while running, got $og"
# Old gen-1 digest must have been cleared during the Running transition.
[[ -z "$(status_field e2e-full '{.status.digest}')" ]] || fail "stale gen1 digest survived into gen2"
wait_phase e2e-full Ready
digest2="$(status_field e2e-full '{.status.digest}')"
[[ "$digest2" != "$digest1" ]] || fail "gen2 digest identical to gen1"
[[ "$(status_field e2e-full '{.status.observedGeneration}')" == "2" ]] || fail "gen2 binding"
[[ "$(job_count e2e-full)" -eq 1 ]] || fail "stale gen1 Job not garbage collected"
go run ./cmd/verifier --context "$CTX" --snapshot e2e-full --namespace "$NS" >/dev/null
pass "generation 2 Ready with distinct digest $digest2; stale Job cleaned"

# ---- 6. deletion waits for dependents ----
info "6. deletion: finalizer drains Job + result ConfigMap"
"$KUBECTL" --context "$CTX" -n "$NS" delete snapshot e2e-full --wait=false
deadline=$((SECONDS+90))
while (( SECONDS < deadline )); do
  if ! "$KUBECTL" --context "$CTX" -n "$NS" get snapshot e2e-full >/dev/null 2>&1; then gone=1; break; fi
  sleep 1
done
[[ "${gone:-0}" == "1" ]] || fail "snapshot not deleted; finalizer stuck"
[[ "$(job_count e2e-full)" -eq 0 ]] || fail "worker Job survived deletion"
"$KUBECTL" --context "$CTX" -n "$NS" get cm -l snapshot.example.com/snapshot=e2e-full -o name | grep -q . \
  && fail "result ConfigMap survived deletion" || true
pass "snapshot + dependents fully removed"

echo
echo "${GREEN}ALL E2E CHECKS PASSED${NC}"
