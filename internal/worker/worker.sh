#!/usr/bin/env bash
# worker.sh runs inside the snapshot worker Job (inside a kind node image,
# which contains bash, coreutils, findutils and kubectl).
#
# It performs REAL work:
#   1. reads the source ConfigMap mounted at /data
#   2. computes a per-file SHA-256 and a chained root SHA-256 with sha256sum
#   3. writes the result to a ConfigMap via the Kubernetes API (kubectl)
#
# The digest construction MUST match internal/digest (Go reference):
#   files are selected by SUB_PATH (or every top-level key, byte-sorted),
#   and the snapshot digest is sha256 over "<filehash>\n" for each file.
set -euo pipefail

DATA_DIR="${DATA_DIR:-/data}"
RESULTS_DIR="$(mktemp -d)"
trap 'rm -rf "$RESULTS_DIR"' EXIT

MANIFEST_FILE="$RESULTS_DIR/manifest"
: > "$MANIFEST_FILE"

if [[ -n "${SUB_PATH:-}" ]]; then
  if [[ ! -e "$DATA_DIR/$SUB_PATH" ]]; then
    echo "file not found: $SUB_PATH" >&2
    exit 2
  fi
  FILES="$SUB_PATH"
else
  # ConfigMap volumes expose each key as a top-level symlink into the
  # timestamped "..YYYY" directory via "..data". We must select only the real
  # key entries: -xtype f matches regular files AND symlinks whose *target* is
  # a regular file, which excludes both the "..YYYY" directories and the
  # "..data" symlink (whose target is a directory). mindepth/maxdepth keep us
  # at the top level.
  FILES="$(cd "$DATA_DIR" && find . -mindepth 1 -maxdepth 1 -xtype f -printf '%P\n' | LC_ALL=C sort)"
fi

while IFS= read -r f; do
  [[ -z "$f" ]] && continue
  hash="$(sha256sum "$DATA_DIR/$f" | awk '{print $1}')"
  size="$(wc -c < "$DATA_DIR/$f" | tr -d ' ')"
  printf '%s  %s  %s\n' "$hash" "$size" "$f" >> "$MANIFEST_FILE"
done <<< "$FILES"

file_count="$(grep -c . "$MANIFEST_FILE" || true)"
total_bytes="$(awk '{s += $2} END {print s+0}' "$MANIFEST_FILE")"
# Canonical manifest text: lines joined with newlines, NO trailing newline.
# Command substitution strips the trailing newline, which matches the Go
# reference exactly; the root digest is sha256 over that text.
manifest="$(cat "$MANIFEST_FILE")"
if [[ -n "$manifest" ]]; then
  digest="$(printf '%s' "$manifest" | sha256sum | awk '{print $1}')"
else
  digest="$(printf '' | sha256sum | awk '{print $1}')"
fi

echo "digest=$digest files=$file_count bytes=$total_bytes"
echo "---- manifest ----"
echo "$manifest"
echo "------------------"

# Publish results as a ConfigMap with deterministic labels + ownerReference.
# `kubectl apply` idempotently handles Job pod retries.
manifest_indented="$(printf '%s\n' "$manifest" | sed 's/^/    /')"
cat <<EOF | kubectl apply --server-side=false -f -
apiVersion: v1
kind: ConfigMap
metadata:
  name: ${RESULT_CONFIGMAP}
  namespace: ${SNAPSHOT_NAMESPACE}
  labels:
    app.kubernetes.io/part-of: recoverable-snapshot-controller
    snapshot.example.com/managed-by: snapshot-controller
    snapshot.example.com/snapshot: ${SNAPSHOT_NAME}
    snapshot.example.com/generation: "${SNAPSHOT_GENERATION}"
  ownerReferences:
  - apiVersion: snapshot.example.com/v1alpha1
    kind: Snapshot
    name: ${SNAPSHOT_NAME}
    uid: ${SNAPSHOT_UID}
    controller: true
    blockOwnerDeletion: true
data:
  algorithm: SHA-256
  digest: "${digest}"
  fileCount: "${file_count}"
  totalBytes: "${total_bytes}"
  sourceConfigMap: "${SOURCE_CONFIGMAP}"
  subPath: "${SUB_PATH:-}"
  generation: "${SNAPSHOT_GENERATION}"
  manifest: |-
${manifest_indented}
EOF

echo "result ConfigMap ${RESULT_CONFIGMAP} published"
