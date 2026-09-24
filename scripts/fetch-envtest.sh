#!/usr/bin/env bash
# Fetch envtest (kube-apiserver/etcd/kubectl) binaries from the
# controller-tools GitHub release when the legacy setup-envtest GCS bucket
# is unreachable (it returns 401 on some networks).
#
# Usage: scripts/fetch-envtest.sh [VERSION]
# Default VERSION=1.31.0. Installs to
#   $HOME/.local/share/envtest-binaries/controller-tools/envtest
# and prints the directory on stdout.
set -euo pipefail

VERSION="${1:-1.31.0}"
OS="$(go env GOOS)"
ARCH="$(go env GOARCH)"
DEST="${HOME}/.local/share/envtest-binaries/controller-tools/envtest"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

URL="https://github.com/kubernetes-sigs/controller-tools/releases/download/envtest-v${VERSION}/envtest-v${VERSION}-${OS}-${ARCH}.tar.gz"
echo ">> downloading $URL" >&2
curl -fsSL --retry 3 -o "$TMP/envtest.tar.gz" "$URL"

mkdir -p "$DEST"
tar -xzf "$TMP/envtest.tar.gz" -C "$TMP"
cp -f "$TMP"/kube-apiserver "$TMP"/etcd "$TMP"/kubectl "$DEST"/ 2>/dev/null || {
  # Newer archives keep a top-level dir; locate binaries instead.
  find "$TMP" -type f \( -name kube-apiserver -o -name etcd -o -name kubectl \) -exec cp -f {} "$DEST"/ \;
}
chmod +x "$DEST"/kube-apiserver "$DEST"/etcd "$DEST"/kubectl

echo "$DEST"
