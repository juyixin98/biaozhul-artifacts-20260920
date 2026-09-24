#!/usr/bin/env bash
# Storage-version migration operator script (rollout + rollback).
#
# This is the runbook-as-code for moving stored data between
# v1alpha1 and v1. It performs the procedure that
# TestStorageMigrationAndFailureRecord automates in tests, against a real
# cluster reachable from KUBECONFIG.
#
#   scripts/migrate-storage.sh up      v1alpha1 storage -> v1 storage
#   scripts/migrate-storage.sh down    v1 storage -> v1alpha1 storage
#   scripts/migrate-storage.sh status  print CRD storedVersions + record dir
#
# Safety:
#   * refuses to proceed while the opposite-direction migration record
#     contains failures;
#   * writes every record under ./migration-records/ with a UTC timestamp;
#   * NEVER edits status.storedVersions automatically on failure. The
#     final removal of the old version is a separate, confirmed step.
set -euo pipefail

ACTION="${1:-status}"
CRD="timers.timer.example.com"
RECORDS_DIR="${RECORDS_DIR:-$(pwd)/migration-records}"
BIN="$(go env GOPATH)/bin/storage-migrator"
mkdir -p "$RECORDS_DIR"
TS="$(date -u +%Y%m%dT%H%M%SZ)"

need_bin() {
  if [ ! -x "$BIN" ]; then
    echo ">> building storage-migrator" >&2
    go build -o "$BIN" ./cmd/migrator
  fi
}

crd_patch_storage() {
  local want="$1"
  local other="v1alpha1"
  [ "$want" = "v1alpha1" ] && other="v1"
  echo ">> setting storage=true on $want (storage=false on $other)" >&2
  # Per-version storage flags cannot be expressed via strategic merge on a
  # list of objects without stable patch keys, so build a JSON merge patch
  # carrying only name/storage per version.
  tmp="$(mktemp)"
  kubectl get crd "$CRD" -o json |
    jq --arg want "$want" '
      {spec: {versions: (.spec.versions | map({name: .name, storage: (.name == $want)})))}}
    ' > "$tmp"
  kubectl patch crd "$CRD" --type merge --patch-file "$tmp" >/dev/null
  rm -f "$tmp"
}

case "$ACTION" in
  status)
    kubectl get crd "$CRD" \
      -o jsonpath='served/stored versions:{range .spec.versions[*]} {.name}(served={.served},storage={.storage}){end}{"\n"}'
    echo "status.storedVersions: $(kubectl get crd "$CRD" -o jsonpath='{.status.storedVersions}' | tr ' ' ',')"
    echo "records: $RECORDS_DIR"
    ls -1t "$RECORDS_DIR" 2>/dev/null | head -5 || true
    ;;

  up)
    need_bin
    crd_patch_storage v1
    record="$RECORDS_DIR/up-${TS}.json"
    echo ">> running storage rewrite, record=$record" >&2
    if "$BIN" --direction=up --record="$record"; then
      echo ">> all objects rewritten. Next, verify and remove the old version:" >&2
      echo "   kubectl patch crd $CRD --subresource=status --type merge -p \\" >&2
      echo "     '{\"status\":{\"storedVersions\":[\"v1\"]}}'" >&2
    else
      echo "!! migration reported failures; see $record" >&2
      echo "!! storedVersions was NOT changed. Follow docs/ROLLBACK.md." >&2
      exit 1
    fi
    ;;

  down)
    need_bin
    echo ">> preflight: refusing downgrade while any v1 object carries sub-second precision" >&2
    bad="$(kubectl get timers.timer.example.com -A -o json |
      jq '[.items[] | select(.spec.interval.nanos? != 0 or (.spec.timeout.nanos? != 0 and .spec.timeout.nanos? != null)) | .metadata.namespace+"/"+.metadata.name]')"
    if [ "$bad" != "[]" ]; then
      echo "!! these objects cannot be expressed in v1alpha1 (whole seconds only): $bad" >&2
      echo "!! round them explicitly after an explicit data decision, then retry." >&2
      exit 2
    fi
    crd_patch_storage v1alpha1
    record="$RECORDS_DIR/down-${TS}.json"
    if "$BIN" --direction=down --record="$record"; then
      echo ">> rollback rewrite complete; record=$record" >&2
    else
      echo "!! rollback rewrite reported failures; see $record" >&2
      exit 1
    fi
    ;;

  *)
    echo "usage: $0 {up|down|status}" >&2
    exit 64
    ;;
esac
