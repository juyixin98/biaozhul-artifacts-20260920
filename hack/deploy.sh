#!/usr/bin/env bash
# Creates the kind cluster, loads the image and installs everything including
# the admission webhooks (CA bundle is produced by the manager itself).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CLUSTER="${CLUSTER:-config-dedup}"
IMAGE="${IMAGE:-config-distributor:dev}"
# All kubectl calls are pinned to our context because development machines may
# host several kind clusters at once.
KCTX="kind-$CLUSTER"
k() { kubectl --context "$KCTX" "$@"; }

echo "==> [1/6] create kind cluster '$CLUSTER' (idempotent)"
if ! kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
  kind create cluster --name "$CLUSTER" --config "$ROOT/hack/kind-config.yaml"
else
  echo "    cluster already exists"
fi
k config use-context "kind-$CLUSTER"

echo "==> [2/6] build and load manager image into kind"
# --network=host lets the build container use the host's DNS/proxy settings to
# reach the Go module proxy.
docker build --network=host -t "$IMAGE" "$ROOT"
kind load docker-image "$IMAGE" --name "$CLUSTER"

echo "==> [3/6] install CRDs, RBAC, namespace"
k apply -f "$ROOT/config/crd/bases/config.example.com_configsnapshots.yaml"
k apply -f "$ROOT/config/crd/bases/chaos.example.com_failpolicies.yaml"
k apply -f "$ROOT/config/rbac/rbac.yaml"

echo "==> [4/6] deploy manager and webhook service"
sed "s#IMAGE_PLACEHOLDER#$IMAGE#g" "$ROOT/config/manager/deployment.yaml" | k apply -f -

echo "==> [5/6] install webhook configs; the manager patches their caBundle itself"
k -n config-system rollout status deploy/config-distributor --timeout=180s
# The manager generates its own CA (real ECDSA+x509) and, once it can GET these
# ValidatingWebhookConfigurations, patches each caBundle via the Kubernetes API.
# Apply with an EMPTY caBundle (valid base64) so the object is accepted; the
# manager then replaces it with the real bundle.
for f in "$ROOT"/config/webhook/*.yaml; do
  sed 's#CABUNDLE_PLACEHOLDER##g' "$f" | k apply -f -
done
for vwc in config-distributor-chaos config-distributor-validate; do
  for i in $(seq 1 30); do
    len="$(k get validatingwebhookconfiguration "$vwc" \
      -o jsonpath='{.webhooks[0].clientConfig.caBundle}' 2>/dev/null | wc -c)"
    [ "$len" -gt 20 ] && break
    sleep 2
  done
  if [ "$len" -le 20 ]; then
    echo "ERROR: caBundle for $vwc was never patched" >&2
    exit 1
  fi
  echo "    $vwc caBundle ready ($len chars)"
done

echo "==> [6/6] install test namespaces and samples"
k apply -f "$ROOT/config/samples/namespaces.yaml"

echo
echo "deploy complete. current status:"
k get configsnapshots -A 2>/dev/null || true
k -n config-system get pods -l app=config-distributor
