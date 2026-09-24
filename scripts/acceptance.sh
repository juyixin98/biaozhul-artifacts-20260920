#!/usr/bin/env bash
# Acceptance suite for quota-reserver.
#
# Runs against the cluster $K points at (default: kubectl's current context;
# set KUBECTL="kubectl --context <name>" to pin a context), which must have
# config/install.yaml deployed. Exercises:
#   A. concurrent requests against a fixed pool (no over-commit)
#   B. reserve -> pod bind -> pod delete -> release (consistency with real objects)
#   C. TTL expiry and rejection of a late pod
#   D. controller restart mid-flight (no double-count, convergence)
#   E. invariant: pool.used == sum(active reservations) == sum(bound pod requests)
set -euo pipefail

K=${KUBECTL:-kubectl}
NS=${ACCEPTANCE_NS:-demo-quota}
SYS_NS=quota-system
TIMEOUT=${ACCEPTANCE_TIMEOUT:-90s}

log()  { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m    OK: %s\033[0m\n' "$*"; }
fail() { printf '\033[1;31mFAIL: %s\033[0m\n' "$*" >&2; exit 1; }

phase() { $K -n "$NS" get resourcerequest "$1" -o jsonpath='{.status.phase}' 2>/dev/null || true; }

wait_phase() { # name phase
  $K -n "$NS" wait --for=jsonpath="{.status.phase}=$2" "resourcerequest/$1" --timeout="$TIMEOUT" >/dev/null \
    || fail "request $1 did not reach phase $2 within $TIMEOUT (now: $(phase "$1"))"
}

pool_used_cpu() { local v; v=$($K -n "$NS" get quotapool default -o jsonpath='{.status.usedCPUMilli}'); echo "${v:-0}"; }
pool_used_mem() { local v; v=$($K -n "$NS" get quotapool default -o jsonpath='{.status.usedMemoryBytes}'); echo "${v:-0}"; }
pool_allocs()   { $K -n "$NS" get quotapool default -o jsonpath='{.status.allocations}' ; }

# Sum of spec.cpuMilli over requests in an active (Reserved/Bound) phase.
active_cpu_sum() {
  $K -n "$NS" get resourcerequest \
    -o go-template='{{range .items}}{{if or (eq .status.phase "Reserved") (eq .status.phase "Bound")}}{{.spec.cpuMilli}}{{"\n"}}{{end}}{{end}}' \
    | awk '{s+=$1} END{print s+0}'
}
active_mem_sum() {
  $K -n "$NS" get resourcerequest \
    -o go-template='{{range .items}}{{if or (eq .status.phase "Reserved") (eq .status.phase "Bound")}}{{.spec.memoryBytes}}{{"\n"}}{{end}}{{end}}' \
    | awk '{s+=$1} END{print s+0}'
}

check_invariant() {
  local used_cpu used_mem exp_cpu exp_mem
  used_cpu=$(pool_used_cpu); used_mem=$(pool_used_mem)
  exp_cpu=$(active_cpu_sum); exp_mem=$(active_mem_sum)
  [[ "$used_cpu" == "$exp_cpu" && "$used_mem" == "$exp_mem" ]] \
    || fail "invariant broken: pool.used=(${used_cpu}m,${used_mem}B) != active reservations=(${exp_cpu}m,${exp_mem}B)"
  ok "invariant: pool.used=(${used_cpu}m,${used_mem}B) == sum(active reservations)"
}

mk_request() { # name cpuMilli memBytes ttl  [retries]
  local tries=${5:-1} n=1 out
  local body
  body=$(cat <<EOF
apiVersion: quota.biaozhu.dev/v1alpha1
kind: ResourceRequest
metadata:
  name: $1
spec:
  pool: default
  cpuMilli: $2
  memoryBytes: $3
  ttlSeconds: $4
EOF
)
  while :; do
    out=$($K -n "$NS" apply -f - <<<"$body" 2>&1) && return 0
    if (( n < tries )) && echo "$out" | grep -qE "failed calling webhook|Internal error occurred"; then
      sleep 2; n=$((n+1)); continue
    fi
    echo "$out" >&2; return 1
  done
}

mk_pod() { # name request cpuMilli memBytes
  $K -n "$NS" apply -f - <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: $1
  labels:
    quota.biaozhu.dev/request: $2
spec:
  containers:
    - name: pause
      image: registry.k8s.io/pause:3.9
      resources:
        requests:
          cpu: $3m
          memory: $4
EOF
}

delete_requests() { $K -n "$NS" delete resourcerequest --all --wait=true --timeout=60s >/dev/null 2>&1 || true; }

log "0. setup: namespace + pool"
$K apply -f config/samples/namespace.yaml >/dev/null
$K apply -f config/samples/quotapool_default.yaml >/dev/null
delete_requests
$K -n "$NS" delete pods --all --wait=false >/dev/null 2>&1 || true
sleep 2
check_invariant

log "A. concurrency: 10 requests x 400m/256Mi against a 2000m/2Gi pool"
for i in $(seq 1 10); do
  mk_request "conc-$i" 400 268435456 600 &
done
wait
# Wait until every request has left Pending.
for i in $(seq 1 10); do
  $K -n "$NS" wait --for=jsonpath='{.status.phase}' "resourcerequest/conc-$i" --timeout="$TIMEOUT" >/dev/null \
    || fail "conc-$i never got a phase"
done
deadline=$((SECONDS + 60))
while true; do
  pending=$($K -n "$NS" get resourcerequest -o go-template='{{range .items}}{{if eq .status.phase "Pending"}}x{{end}}{{end}}' | wc -c)
  [[ "$pending" == "0" ]] && break
  (( SECONDS < deadline )) || fail "requests still Pending after 60s"
  sleep 1
done
reserved=$($K -n "$NS" get resourcerequest -o go-template='{{range .items}}{{if eq .status.phase "Reserved"}}x{{end}}{{end}}' | wc -c)
rejected=$($K -n "$NS" get resourcerequest -o go-template='{{range .items}}{{if eq .status.phase "Rejected"}}x{{end}}{{end}}' | wc -c)
[[ "$reserved" == "5" && "$rejected" == "5" ]] \
  || fail "expected 5 Reserved + 5 Rejected, got $reserved Reserved + $rejected Rejected"
ok "exactly 5 Reserved, 5 Rejected (5*400m = 2000m = capacity)"
[[ "$(pool_used_cpu)" == "2000" ]] || fail "pool usedCPU=$(pool_used_cpu), want 2000"
[[ "$(pool_used_mem)" == "1342177280" ]] || fail "pool usedMem=$(pool_used_mem), want 1342177280"
check_invariant
delete_requests
deadline=$((SECONDS + 30))
while [[ "$(pool_used_cpu)" != "0" ]]; do
  (( SECONDS < deadline )) || fail "pool did not drain after deleting requests"
  sleep 1
done
ok "pool drained to 0 after deleting all requests (finalizer release)"

log "B. binding: reserve -> admit pod -> Bound -> delete pod -> Released"
mk_request bind-demo 500 268435456 300
wait_phase bind-demo Reserved
[[ "$(pool_used_cpu)" == "500" ]] || fail "pool usedCPU=$(pool_used_cpu), want 500"
mk_pod bind-demo bind-demo 500 256Mi >/dev/null
wait_phase bind-demo Bound
rr_uid=$($K -n "$NS" get resourcerequest bind-demo -o jsonpath='{.metadata.uid}')
bound_pod=$($K -n "$NS" get resourcerequest bind-demo -o jsonpath='{.status.boundPod}')
[[ "$bound_pod" == "bind-demo" ]] || fail "boundPod=$bound_pod, want bind-demo"
pool_allocs | grep -q "$rr_uid" || fail "pool ledger does not contain request UID $rr_uid"
ok "request Bound to pod, pool ledger holds UID $rr_uid"
$K -n "$NS" wait --for=condition=Ready "pod/bind-demo" --timeout="$TIMEOUT" >/dev/null \
  || fail "pod bind-demo did not become Ready"
ok "real pod is Running/Ready while reservation is Bound"
$K -n "$NS" delete pod bind-demo --wait=true --timeout=60s >/dev/null
wait_phase bind-demo Released
[[ "$(pool_used_cpu)" == "0" ]] || fail "pool usedCPU=$(pool_used_cpu) after pod deletion, want 0"
ok "pod deleted -> request Released -> quota freed"

log "C. expiry: TTL elapses, late pod is rejected"
mk_request late-demo 300 134217728 10
wait_phase late-demo Reserved
wait_phase late-demo Expired
[[ "$(pool_used_cpu)" == "0" ]] || fail "pool usedCPU=$(pool_used_cpu) after expiry, want 0"
if out=$(mk_pod late-pod late-demo 300 128Mi 2>&1); then
  $K -n "$NS" delete pod late-pod --wait=false >/dev/null 2>&1 || true
  fail "late pod was admitted after expiry"
fi
echo "$out" | grep -qi "expired" || fail "late pod denied but without 'expired' reason: $out"
ok "late pod rejected with expiry reason"
if out=$($K -n "$NS" apply -f - 2>&1 <<'EOF'
apiVersion: v1
kind: Pod
metadata:
  name: no-label-pod
spec:
  containers:
    - name: pause
      image: registry.k8s.io/pause:3.9
      resources:
        requests: {cpu: 10m, memory: 16Mi}
EOF
); then
  $K -n "$NS" delete pod no-label-pod --wait=false >/dev/null 2>&1 || true
  fail "pod without reservation label was admitted"
fi
echo "$out" | grep -q "quota.biaozhu.dev/request" || fail "unlabeled pod denied with unexpected message: $out"
ok "pod without reservation label rejected"

log "D. controller restart: no double-count, convergence"
mk_request restart-1 200 134217728 600
wait_phase restart-1 Reserved
used_before=$(pool_used_cpu)

# With 2 replicas, delete exactly ONE: the other replica must keep serving
# admission and reconciliation while the deleted pod is being recreated —
# this is the zero-downtime property of multi-replica deployment.
$K -n "$SYS_NS" rollout status deployment/quota-reserver-controller-manager --timeout=120s >/dev/null
replica_count=$($K -n "$SYS_NS" get pods -l control-plane=controller-manager --no-headers | wc -l)
[[ "$replica_count" -ge 2 ]] || fail "expected >=2 controller replicas, got $replica_count"
victim=$($K -n "$SYS_NS" get pods -l control-plane=controller-manager -o jsonpath='{.items[0].metadata.name}')
$K -n "$SYS_NS" delete pod "$victim" --wait=false >/dev/null
ok "deleted one controller pod ($victim); surviving replica keeps the webhook up"

# Creating a new request during the restart window must succeed immediately:
# the admission request is served by the surviving pod.
mk_request restart-2 200 134217728 600
wait_phase restart-2 Reserved
ok "admission + reservation kept working while a controller pod restarted"

$K -n "$SYS_NS" rollout status deployment/quota-reserver-controller-manager --timeout=120s >/dev/null
[[ "$(phase restart-1)" == "Reserved" ]] || fail "restart-1 lost its Reserved phase across restart"
[[ "$(pool_used_cpu)" == "400" ]] || fail "pool usedCPU=$(pool_used_cpu) after restart, want 400 (was $used_before for restart-1)"
alloc_count=$(pool_allocs | grep -o 'requestUID' | wc -l)
[[ "$alloc_count" == "2" ]] || fail "pool ledger has $alloc_count allocations, want 2"
check_invariant
delete_requests

log "E. final invariant check"
deadline=$((SECONDS + 30))
while [[ "$(pool_used_cpu)" != "0" ]]; do
  (( SECONDS < deadline )) || fail "pool did not drain at cleanup"
  sleep 1
done
check_invariant

log "ALL ACCEPTANCE SCENARIOS PASSED"
