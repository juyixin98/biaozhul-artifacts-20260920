#!/usr/bin/env bash
# deploy.sh — build the image, create the kind cluster, install CRDs, webhook
# certs and the controller. Fully local: uses cached kindest/node and base
# images and `kind load docker-image` (no registry push).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$SCRIPT_DIR"

CLUSTER="quota-reservation"
IMAGE="quota-controller:dev"
KIND_NODE="kindest/node:v1.30.10"

need() { command -v "$1" >/dev/null 2>&1 || { echo "missing binary: $1" >&2; exit 1; }; }
need docker; need kind; need kubectl; need openssl

# 1. Cluster ------------------------------------------------------------------
if kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
    echo "[deploy] kind cluster $CLUSTER already exists"
else
    echo "[deploy] creating kind cluster $CLUSTER ($KIND_NODE)"
    kind create cluster --name "$CLUSTER" --image "$KIND_NODE" \
        --config hack/kind-config.yaml
fi
kubectl cluster-info --context "kind-$CLUSTER" >/dev/null
kubectl config use-context "kind-$CLUSTER" >/dev/null

# 2. Image --------------------------------------------------------------------
# Build a static linux/amd64 binary on the host (local module cache), then
# package it — `docker build` itself needs no network access.
echo "[deploy] compiling manager (static linux/amd64)"
mkdir -p bin
GOOS=linux GOARCH=amd64 CGO_ENABLED=0 \
    go build -trimpath -ldflags '-s -w' -o bin/manager ./cmd/manager
echo "[deploy] building image $IMAGE"
docker build -t "$IMAGE" .
echo "[deploy] loading image into kind nodes"
# kind v0.23 cannot auto-detect the containerd v2 snapshotter, so use
# `docker save | ctr images import` directly inside the node. The image lands
# as docker.io/library/<name>; the Deployment references that full path.
node="quota-reservation-control-plane"
if docker exec "$node" ctr -n k8s.io images ls | grep -q "docker.io/library/${IMAGE%%:*}:${IMAGE##*:}"; then
    echo "[deploy] image already present on node"
else
    docker save "$IMAGE" | docker exec -i "$node" ctr -n k8s.io images import -
fi

# 3. Certs --------------------------------------------------------------------
echo "[deploy] generating webhook certificates"
OUTDIR=hack/_output bash hack/gen-certs.sh
CA_BUNDLE="$(cat hack/_output/webhook-ca-bundle.txt)"

# 4. Namespace + certs --------------------------------------------------------
kubectl apply -f config/rbac/rbac.yaml
kubectl apply -f hack/_output/tls-secret.yaml

# 5. CRDs ---------------------------------------------------------------------
echo "[deploy] installing CRDs"
kubectl apply -f config/crd/quota.example.com_resourceclaims.yaml
kubectl apply -f config/crd/quota.example.com_reservationpools.yaml

# 6. Webhooks (inject the real CA bundle) -------------------------------------
echo "[deploy] installing webhooks"
sed "s/__CA_BUNDLE__/${CA_BUNDLE}/g" config/webhook/manifests.yaml \
    | kubectl apply -f -

# 7. Controller ---------------------------------------------------------------
echo "[deploy] deploying controller"
kubectl apply -f config/manager/deployment.yaml
kubectl -n quota-system rollout status deployment/quota-controller --timeout=120s

echo "[deploy] SUCCESS. Try: kubectl apply -f config/samples/quota-namespace.yaml"
