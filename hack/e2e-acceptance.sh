#!/usr/bin/env bash
# e2e-acceptance.sh — runs the acceptance scenarios against the live kind
# cluster created by hack/deploy.sh. Prints PASS/FAIL per scenario and exits
# non-zero on any failure.
set -uo pipefail

NS="quota-demo"
PASS=0; FAIL=0
ok()   { echo "  PASS: $1"; PASS=$((PASS+1)); }
bad()  { echo "  FAIL: $1" >&2; FAIL=$((FAIL+1)); }
wait_phase() { # <claim> <phase> [timeout-seconds]
  local claim="$1" want="$2" timeout="${3:-20}"
  local deadline=$((SECONDS+timeout))
  while (( SECONDS < deadline )); do
    local phase
    phase="$(kubectl -n "$NS" get resourceclaim "$claim" \
        -o jsonpath='{.status.phase}' 2>/dev/null || true)"
    if [[ "$phase" == "$want" ]]; then return 0; fi
    sleep 0.5
  done
  return 1
}
pool_cpu() { kubectl -n "$NS" get reservationpool default-pool \
    -o jsonpath='{.status.reservedCPU}' 2>/dev/null || true; }

# wait_apiserver blocks until the apiserver answers a live call; used after a
# controller rollout where this single-node kind control plane can briefly
# refuse connections.
wait_apiserver() {
  local deadline=$((SECONDS+30))
  while (( SECONDS < deadline )); do
    if kubectl get --raw /healthz >/dev/null 2>&1; then
      kubectl -n "$NS" get reservationpool default-pool >/dev/null 2>&1 && return 0
    fi
    sleep 1
  done
  return 1
}

kubectl config use-context kind-quota-reservation >/dev/null
kubectl cluster-info >/dev/null || { echo "cluster not reachable; run hack/deploy.sh first" >&2; exit 2; }
echo "== e2e acceptance on context kind-quota-reservation =="

echo "[1] install namespace + pool + demo claims"
kubectl apply -f config/samples/quota-namespace.yaml
kubectl apply -f config/samples/resourceclaims.yaml

echo "[2] normal claim becomes Reserved and pool ledger matches"
if wait_phase claim-normal Reserved; then ok "claim-normal Reserved"; else bad "claim-normal Reserved"; fi
# Other claims were applied in the same batch, so check THIS claim's own
# contribution in the durable ledger rather than the pool-wide total.
uid="$(kubectl -n "$NS" get resourceclaim claim-normal -o jsonpath='{.metadata.uid}')"
entry_cpu="$(kubectl -n "$NS" get reservationpool default-pool -o jsonpath="{.status.claims.$uid.cpu}")"
[[ "$entry_cpu" == "500" ]] \
  && ok "ledger entry for claim-normal = 500 milli-cpu" \
  || bad "ledger entry for claim-normal = '${entry_cpu}' want 500"

echo "[3] over-capacity claim is Rejected (never debited)"
if wait_phase claim-too-big Rejected; then ok "claim-too-big Rejected"; else
  echo "   phase=$(kubectl -n "$NS" get resourceclaim claim-too-big -o jsonpath='{.status.phase}') msg=$(kubectl -n "$NS" get resourceclaim claim-too-big -o jsonpath='{.status.message}')"
  bad "claim-too-big Rejected"; fi

echo "[4] webhook rejects bad units / over-limit at admission"
for spec in 'cpu:"65000m",memory:"128Mi",ttl:"5m"' 'cpu:"500m",memory:"500Gi",ttl:"5m"' 'cpu:"bad",memory:"128Mi",ttl:"5m"'; do
  if echo "apiVersion: quota.example.com/v1alpha1
kind: ResourceClaim
metadata: {name: bad-input, namespace: $NS}
spec: {$spec}" | kubectl apply --validate=true -f - 2>/tmp/wh-err; then
    kubectl -n "$NS" delete resourceclaim bad-input --ignore-not-found >/dev/null 2>&1
    bad "spec {$spec} should be denied"
  else
    ok "denied invalid spec (${spec%%,*})"
  fi
done

echo "[5] spec immutability"
# get -o yaml renders cpu WITHOUT quotes (cpu: 500m), so match accordingly.
kubectl -n "$NS" get resourceclaim claim-normal -o yaml | sed 's/cpu: 500m/cpu: 900m/' | kubectl apply -f - >/tmp/imm 2>&1 && bad "spec edit unexpectedly allowed" || ok "spec edit denied"

echo "[6] binding a real Pod flips the claim to Bound; ledger unchanged"
kubectl apply -f config/samples/pods.yaml
if wait_phase claim-normal Bound; then ok "claim-normal Bound to workload-normal"; else bad "claim-normal Bound"; fi
bound_uid="$(kubectl -n "$NS" get resourceclaim claim-normal -o jsonpath='{.metadata.uid}')"
bound_entry="$(kubectl -n "$NS" get reservationpool default-pool -o jsonpath="{.status.claims.$bound_uid.cpu}")"
[[ "$bound_entry" == "500" ]] \
  && ok "claim-normal ledger contribution stays 500m while bound" \
  || bad "claim-normal ledger changed after bind: ${bound_entry}"

echo "[7] oversized Pod rejected at admission"
if kubectl apply -f config/samples/pod-oversized.yaml 2>/tmp/pod-err; then
  kubectl -n "$NS" delete pod workload-oversized --ignore-not-found >/dev/null
  bad "oversized pod admitted"
else
  ok "oversized pod denied ($(grep -o 'must not be larger than[^"]*\|requests [0-9m]* cpu' /tmp/pod-err | head -1))"
fi

echo "[8] late Pod after expiry is rejected and capacity is returned"
wait_phase claim-short Expired 40 \
  && ok "claim-short Expired" || bad "claim-short Expired (phase=$(kubectl -n "$NS" get resourceclaim claim-short -o jsonpath='{.status.phase}'))"
# Pool cpu: normal(500m, bound) + second(1000m) = 1500m after short's 200m released.
# Compare integer millis via jsonpath arithmetic is awkward; assert the raw
# canonical string excludes short's "200m" share by checking second+normal.
if echo '{"claim-normal":"500m"}' >/dev/null; then
  cpu="$(pool_cpu)"
  case "$cpu" in
    "1500m"|"1500"|1500m) ok "pool cpu after short expiry = $cpu (=500+1000)" ;;
    *)
      milli="$(kubectl -n "$NS" get reservationpool default-pool -o jsonpath='{.status.reservedCPU}')"
      bad "pool cpu after short expiry = '$milli' want 1500m" ;;
  esac
fi
if kubectl -n "$NS" run late-pod --image=registry.k8s.io/pause:3.10 \
      --labels=quota.example.com/claim=claim-short \
      --requests=cpu=50m,memory=16Mi --restart=Never 2>/tmp/late-err; then
  kubectl -n "$NS" delete pod late-pod --ignore-not-found >/dev/null
  bad "late pod admitted against expired claim"
else
  ok "late pod denied after expiry"
fi

echo "[9b] controller rollout restart is idempotent (no double debit)"
wait_apiserver
ledger_before="$(kubectl -n "$NS" get reservationpool default-pool -o jsonpath='{.status.reservedCPU}:{.status.reservedMemory}')"
entries_before="$(kubectl -n "$NS" get reservationpool default-pool -o json | jq '.status.claims | length')"
old_pod="$(kubectl -n quota-system get pod -l app.kubernetes.io/name=quota-controller -o jsonpath='{.items[0].metadata.name}')"
kubectl -n quota-system rollout restart deployment/quota-controller >/dev/null
kubectl -n quota-system rollout status deployment/quota-controller --timeout=120s >/dev/null \
  && ok "controller rolled out (restarted pod $old_pod)" || bad "controller rollout"
wait_apiserver || bad "apiserver/pool not reachable after restart"
new_pod="$(kubectl -n quota-system get pod -l app.kubernetes.io/name=quota-controller \
  --field-selector=status.phase=Running -o jsonpath='{.items[0].metadata.name}')"
[[ -n "$new_pod" && "$new_pod" != "$old_pod" ]] && ok "controller pod actually changed: $old_pod -> $new_pod" || bad "controller pod did not change (got '$new_pod')"
# Give the freshly started informers time to relist and reconcile.
sleep 4
ledger_after="$(kubectl -n "$NS" get reservationpool default-pool -o jsonpath='{.status.reservedCPU}:{.status.reservedMemory}')"
entries_after="$(kubectl -n "$NS" get reservationpool default-pool -o json | jq '.status.claims | length')"
[[ -n "$ledger_after" && "$ledger_before" == "$ledger_after" ]] \
  && ok "pool totals unchanged across restart ($ledger_after)" \
  || bad "pool totals changed across restart: '$ledger_before' -> '$ledger_after'"
[[ -n "$entries_after" && "$entries_before" == "$entries_after" ]] \
  && ok "ledger entry count unchanged across restart ($entries_after)" \
  || bad "ledger entries changed across restart: '$entries_before' -> '$entries_after'"

echo "[9] concurrent applications never overcommit"
kubectl apply -f config/samples/concurrent-claims.yaml
sleep 3
reserved=$(kubectl -n "$NS" get resourceclaims -o json | jq -r '[.items[] | select(.status.phase=="Reserved" or .status.phase=="Bound")] | length')
rejected=$(kubectl -n "$NS" get resourceclaims -o json | jq -r '[.items[] | select(.status.phase=="Rejected")] | length')
echo "   reserved/bound=$reserved rejected=$rejected"
# Pre-existing at this point: claim-normal(500m,Bound) + claim-second(1000m)
# = 1500m (claim-short's 200m was released at expiry). Remaining 2500m.
# Each race claim wants 600m -> only 4 of the 8 fit (2400m <= 2500m).
# Active total = 6 (2 pre-existing + 4 races); rejected = 5
# (claim-too-big + 4 races). Pool used = 1500 + 2400 = 3900m.
[[ "$reserved" == "6" ]] && ok "exactly 6 active claims after 8 concurrent applications" || bad "active claims = $reserved want 6"
[[ "$rejected" == "5" ]] && ok "exactly 5 rejected (4 races + 1 too-big)" || bad "rejected = $rejected want 5"
[[ "$(pool_cpu)" =~ ^(3900m|3900)$ ]] && ok "pool cpu = $(pool_cpu), never over 4000m" || bad "pool cpu = $(pool_cpu) want 3900m"

echo "[10] finalizers release capacity on delete"
kubectl -n "$NS" delete resourceclaim claim-second --wait=true --timeout=20s >/dev/null 2>&1 \
  && ok "claim-second deleted (finalizer ran)" || bad "claim-second deletion stuck"
sleep 1
[[ "$(pool_cpu)" =~ ^(2900m|2900)$ ]] && ok "pool cpu = $(pool_cpu) after delete (3900-1000)" || bad "pool cpu after delete = $(pool_cpu) want 2900m"

echo
echo "== RESULT: $PASS passed, $FAIL failed =="
exit "$((FAIL>0))"
