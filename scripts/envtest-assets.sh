#!/usr/bin/env bash
# Prints a KUBEBUILDER_ASSETS path containing etcd + kube-apiserver.
# Uses setup-envtest when its GCS listing works; otherwise falls back to a
# direct download from the controller-tools GitHub release, which is enough
# behind restrictive proxies that reject anonymous GCS bucket listing.
set -euo pipefail

K8S_VERSION_PIN="1.30.10"
GITHUB_ENVTEST_VERSION="v1.30.0"   # envtest binaries track the minor line
BIN_DIR="$(go env GOPATH)/bin/envtest-bin"
ASSET_DIR="$BIN_DIR/k8s/${K8S_VERSION_PIN}-linux-amd64"

mkdir -p "$ASSET_DIR"

if [[ -x "$ASSET_DIR/kube-apiserver" && -x "$ASSET_DIR/etcd" ]]; then
  echo "$ASSET_DIR"
  exit 0
fi

SETUP_ENVTEST="$(go env GOPATH)/bin/setup-envtest"
if [[ ! -x "$SETUP_ENVTEST" ]]; then
  GOBIN="$(go env GOPATH)/bin" go install sigs.k8s.io/controller-runtime/tools/setup-envtest@release-0.18 || true
fi

if path="$("$SETUP_ENVTEST" use 1.30.x --bin-dir "$BIN_DIR" -p path 2>/dev/null)"; then
  echo "$path"
  exit 0
fi

echo "setup-envtest could not fetch assets (GCS blocked?); downloading from GitHub..." >&2
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
arch="$(uname -m)"
case "$arch" in
  x86_64) arch=amd64 ;;
  aarch64) arch=arm64 ;;
esac
url="https://github.com/kubernetes-sigs/controller-tools/releases/download/envtest-${GITHUB_ENVTEST_VERSION}/envtest-${GITHUB_ENVTEST_VERSION}-linux-${arch}.tar.gz"
curl -fsSL "$url" -o "$tmp/envtest.tar.gz"
tar xzf "$tmp/envtest.tar.gz" -C "$ASSET_DIR" --strip-components=2
chmod +x "$ASSET_DIR"/etcd "$ASSET_DIR"/kube-apiserver "$ASSET_DIR"/kubectl
echo "$ASSET_DIR"
