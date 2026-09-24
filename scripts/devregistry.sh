#!/usr/bin/env bash
# Provisions a local Docker registry reachable from a kind cluster at
# "localhost:5000" — the official kind local-registry pattern
# (https://kind.sigs.k8s.io/docs/user/local-registry/).
#
# Why a registry instead of `kind load`: on hosts whose Docker uses the
# containerd image store (Storage Driver: overlayfs / io.containerd.snapshotter),
# `kind load` fails with "failed to detect containerd snapshotter". Pushing to
# a local registry works regardless of the host storage driver and needs no
# external network.
set -euo pipefail

CLUSTER="${1:-snapshot-p081a}"
CTX="kind-${CLUSTER}"
reg_name='kind-registry'
reg_port='5000'

# 1. Run the registry (idempotent).
if [ "$(docker inspect -f '{{.State.Running}}' "${reg_name}" 2>/dev/null || true)" != 'true' ]; then
  docker run -d --restart=always -p "127.0.0.1:${reg_port}:5000" \
    --network bridge --name "${reg_name}" registry:2 >/dev/null
fi

# 2. Register the registry with the node's containerd, then restart it.
#    Kind >=0.11 clusters use config_path=/etc/containerd/certs.d.
#
#    We address the registry by its IP on the "kind" network rather than the
#    "kind-registry" hostname: nodes on hosts that inject HTTP_PROXY inherit a
#    no_proxy that exempts the pod/service CIDRs (incl. 172.25.0.0/16) but not
#    arbitrary hostnames, so a hostname would be sent through a proxy the node
#    cannot reach. The IP lives in the exempt range and pulls directly.
reg_ip="$(docker inspect -f '{{.NetworkSettings.Networks.kind.IPAddress}}' "${reg_name}")"
for node in $(kind get nodes --name "${CLUSTER}"); do
  docker exec "${node}" mkdir -p /etc/containerd/certs.d/localhost:${reg_port}
  cat <<EOF | docker exec -i "${node}" cp /dev/stdin /etc/containerd/certs.d/localhost:${reg_port}/hosts.toml
server = "http://${reg_ip}:${reg_port}"

[host."http://${reg_ip}:${reg_port}"]
  capabilities = ["pull", "resolve"]
EOF
done

# 3. Ensure the registry is attached to the kind network so the IP above is
#    routable from nodes.
if [ "$(docker inspect -f='{{json .NetworkSettings.Networks.kind}}' "${reg_name}" 2>/dev/null || true)" = 'null' ]; then
  docker network connect "kind" "${reg_name}" || true
fi

# 4. Document the local registry so tooling is aware (best-effort).
kubectl --context "$CTX" create namespace kube-local-infrastructure >/dev/null 2>&1 || true
cat <<EOF | kubectl --context "$CTX" apply -f -
apiVersion: v1
kind: ConfigMap
metadata:
  name: local-registry-hosting
  namespace: kube-local-infrastructure
data:
  localRegistryHosting.v1: |
    host: "localhost:${reg_port}"
    help: "https://kind.sigs.k8s.io/docs/user/local-registry/"
EOF

echo "local registry ready at localhost:${reg_port} (cluster ${CLUSTER})"
