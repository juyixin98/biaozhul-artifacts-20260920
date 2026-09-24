#!/usr/bin/env bash
# End-to-end: build the image, create a kind cluster, deploy, run acceptance.
set -euo pipefail
cd "$(dirname "$0")/.."

CLUSTER_NAME=${CLUSTER_NAME:-quota-reserver-e2e}
IMG=${IMG:-quota-reserver:e2e}

echo "==> ensuring kind cluster: $CLUSTER_NAME"
if ! kind get clusters | grep -qx "$CLUSTER_NAME"; then
  kind create cluster --name "$CLUSTER_NAME" --wait 120s
fi
# Pin every call to this cluster's context instead of relying on (or
# changing) the shared current-context.
K="kubectl --context kind-$CLUSTER_NAME"

echo "==> building image $IMG"
# Some docker setups cannot reach the network from build containers;
# fall back to host networking if the plain build fails.
docker build -t "$IMG" . || docker build --network=host -t "$IMG" .

echo "==> loading image into kind"
# kind load can fail with a containerd-image-store "content digest not found"
# error on newer docker; fall back to a direct containerd import.
if ! kind load docker-image "$IMG" --name "$CLUSTER_NAME"; then
  echo "    kind load failed, importing directly into node containerd"
  docker save "$IMG" | docker exec -i "$CLUSTER_NAME-control-plane" \
    ctr --namespace k8s.io images import -
fi

echo "==> preloading test pod image (registry.k8s.io/pause:3.9)"
# Kind nodes often cannot reach the public registry directly (proxy/no
# route). Pull on the host, then push into the node's containerd.
docker pull registry.k8s.io/pause:3.9
if ! docker exec "$CLUSTER_NAME-control-plane" crictl image | grep -q "registry.k8s.io/pause.*3.9"; then
  kind load docker-image registry.k8s.io/pause:3.9 --name "$CLUSTER_NAME" 2>/dev/null || \
    docker save registry.k8s.io/pause:3.9 | \
      docker exec -i "$CLUSTER_NAME-control-plane" ctr --namespace k8s.io images import -
fi

echo "==> deploying"
$K apply -f config/install.yaml
$K -n quota-system set image deployment/quota-reserver-controller-manager "manager=$IMG"
# The tag may be unchanged across runs while the image content changed; force
# a new pod so the freshly loaded image actually runs and re-provisions PKI.
$K -n quota-system rollout restart deployment/quota-reserver-controller-manager
$K -n quota-system rollout status deployment/quota-reserver-controller-manager --timeout=180s

# Wait for the manager to publish its CA bundle into the webhook config.
for _ in $(seq 1 30); do
  cab=$($K get validatingwebhookconfiguration quota-reserver-validating-webhook \
    -o jsonpath='{.webhooks[0].clientConfig.caBundle}' 2>/dev/null || true)
  [[ -n "$cab" ]] && break
  sleep 1
done
[[ -n "$cab" ]] || { echo "webhook caBundle was never populated"; exit 1; }

echo "==> running acceptance suite"
KUBECTL="$K" ./scripts/acceptance.sh
