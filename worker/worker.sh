#!/bin/sh
# snapshot-worker produces a real snapshot archive for one Snapshot generation.
#
# It:
#   1. reads the source ConfigMap/Secret from the Kubernetes API with the Pod's
#      service-account token (real authenticated HTTPS protocol operation),
#   2. materializes every key to an exact byte-for-byte file under /work/src,
#   3. packs them into a real tar archive (gzip when the name ends .gz),
#   4. computes the real SHA-256 digest and byte size of the archive and of
#      every input file (sha256sum over the real bytes),
#   5. publishes result.json into the generation's result ConfigMap via the
#      API (POST create; on conflict, PUT with the live resourceVersion).
#
# The controller rejects malformed results or bad digest shapes, so none of
# these values can be faked.
set -eu

log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) [worker] $*"; }
die() { log "ERROR: $*" >&2; exit 1; }

: "${SNAPSHOT_NAME:?}"; : "${SNAPSHOT_NAMESPACE:?}"; : "${SNAPSHOT_GENERATION:?}"
: "${SOURCE_KIND:?}"; : "${SOURCE_NAME:?}"; : "${OUTPUT_NAME:?}"; : "${RESULT_NAME:?}"

WORK=/work
SRC="$WORK/src"
rm -rf "$SRC"; mkdir -p "$SRC"

APISERVER="https://${KUBERNETES_SERVICE_HOST}:${KUBERNETES_SERVICE_PORT}"
TOKEN=$(cat /var/run/secrets/kubernetes.io/serviceaccount/token)
CACERT=/var/run/secrets/kubernetes.io/serviceaccount/ca.crt

api() {
  # api METHOD PATH [BODY]
  if [ "$#" -eq 3 ]; then
    curl -sS --fail-with-body --cacert "$CACERT" -H "Authorization: Bearer $TOKEN" \
      -H "Content-Type: application/json" -H "Accept: application/json" \
      -X "$1" "$APISERVER$2" --data-binary "$3"
  else
    curl -sS --fail-with-body --cacert "$CACERT" -H "Authorization: Bearer $TOKEN" \
      -H "Accept: application/json" -X "$1" "$APISERVER$2"
  fi
}

log "snapshot=$SNAPSHOT_NAME generation=$SNAPSHOT_GENERATION source=$SOURCE_KIND/$SOURCE_NAME"

# 1. Fetch the source object and the owner Snapshot.
case "$SOURCE_KIND" in
  ConfigMap) SRC_PATH="/api/v1/namespaces/$SNAPSHOT_NAMESPACE/configmaps/$SOURCE_NAME" ;;
  Secret)    SRC_PATH="/api/v1/namespaces/$SNAPSHOT_NAMESPACE/secrets/$SOURCE_NAME" ;;
  *) die "unsupported SOURCE_KIND=$SOURCE_KIND" ;;
esac
SRC_JSON=$(api GET "$SRC_PATH") || die "failed to read source $SOURCE_KIND/$SOURCE_NAME"
SNAP_JSON=$(api GET "/apis/snapshot.example.com/v1alpha1/namespaces/$SNAPSHOT_NAMESPACE/snapshots/$SNAPSHOT_NAME") \
  || die "failed to read owner Snapshot"
OWNER_UID=$(printf '%s' "$SNAP_JSON" | jq -r '.metadata.uid')
[ -n "$OWNER_UID" ] && [ "$OWNER_UID" != "null" ] || die "empty owner UID"

# 2. Materialize exact payload.
#    ConfigMap.data values are raw strings -> encode to base64.
#    Secret.data values are already base64 -> pass through.
#    ConfigMap keys are restricted to [-.A-Za-z0-9], so line-oriented parsing
#    is unambiguous and POSIX sh portable (no bash-only `read -d ''`).
KEYS=$(printf '%s' "$SRC_JSON" | jq -r '
  (if "'"$SOURCE_KIND"'" == "ConfigMap"
     then ((.data // {}) * (.binaryData // {}))
     else (.data // {}) end) | keys[]')
COUNT=0
OLDIFS=$IFS
IFS='
'
for KEY in $KEYS; do
  IFS=$OLDIFS
  case "$KEY" in
    *..* | /*) die "unsafe key: $KEY" ;;
  esac
  OUT="$SRC/$KEY"
  mkdir -p "$(dirname "$OUT")"
  if [ "$SOURCE_KIND" = "ConfigMap" ]; then
    printf '%s' "$SRC_JSON" | jq -rj --arg k "$KEY" \
      '((.data // {}) * (.binaryData // {}))[$k] | tostring | @base64' | base64 -d > "$OUT"
  else
    printf '%s' "$SRC_JSON" | jq -rj --arg k "$KEY" '.data[$k] | tostring' | base64 -d > "$OUT"
  fi
  COUNT=$((COUNT+1))
  log "materialized $KEY ($(wc -c < "$OUT" | tr -d ' ') bytes)"
  IFS='
'
done
IFS=$OLDIFS

[ "$COUNT" -gt 0 ] || die "source $SOURCE_KIND/$SOURCE_NAME contains no data keys"

# 3. Pack into a real archive.
ARCHIVE="$WORK/$OUTPUT_NAME"
if printf '%s' "$OUTPUT_NAME" | grep -q '\.gz$'; then
  tar -C "$SRC" -czf "$ARCHIVE" .
else
  tar -C "$SRC" -cf "$ARCHIVE" .
fi
ARCHIVE_SIZE=$(wc -c < "$ARCHIVE" | tr -d ' ')
ARCHIVE_SHA=$(sha256sum "$ARCHIVE" | awk '{print $1}')
log "archive $OUTPUT_NAME size=$ARCHIVE_SIZE sha256=$ARCHIVE_SHA"

# 4. Per-file digests -> result.json (all digests computed over real bytes).
FILE_LIST=$(cd "$SRC" && find . -type f | sort)
FILES_JSON=$(OLDIFS=$IFS; IFS='
'
first=1
printf '['
for f in $FILE_LIST; do
  IFS=$OLDIFS
  rel=${f#./}
  sz=$(wc -c < "$SRC/$rel" | tr -d ' ')
  sha=$(sha256sum "$SRC/$rel" | awk '{print $1}')
  [ $first -eq 1 ] || printf ','
  first=0
  jq -nc --arg p "$rel" --argjson s "$sz" --arg h "$sha" \
    '{path:$p,size:$s,sha256:$h}'
  IFS='
'
done
IFS=$OLDIFS
printf ']')

RESULT="$WORK/result.json"
jq -nc \
  --arg api "snapshot.example.com/v1alpha1" \
  --arg kind "SnapshotResult" \
  --argjson gen "$SNAPSHOT_GENERATION" \
  --arg source "$SOURCE_KIND/$SOURCE_NAME" \
  --arg out "$OUTPUT_NAME" \
  --arg sha "$ARCHIVE_SHA" \
  --argjson size "$ARCHIVE_SIZE" \
  --argjson files "$FILES_JSON" \
  --arg now "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  '{apiVersion:$api,kind:$kind,generation:$gen,source:$source,outputFile:$out,
    sha256:$sha,sizeBytes:$size,files:$files,computedAt:$now}' > "$RESULT"
log "result:"; cat "$RESULT"

# 5. Publish the result ConfigMap (real write through the API).
BODY=$(jq -nc \
  --arg name "$RESULT_NAME" \
  --arg gen "$SNAPSHOT_GENERATION" \
  --arg snap "$SNAPSHOT_NAME" \
  --arg uid "$OWNER_UID" \
  --rawfile data "$RESULT" \
  '{
     apiVersion:"v1", kind:"ConfigMap",
     metadata:{
       name:$name,
       labels:{
         "snapshot.example.com/component":"snapshot-result",
         "snapshot.example.com/generation":$gen
       },
       ownerReferences:[{
         apiVersion:"snapshot.example.com/v1alpha1",
         kind:"Snapshot", name:$snap, uid:$uid,
         controller:true, blockOwnerDeletion:false
       }]
     },
     data:{ "result.json": $data }
   }')

PATH_CM="/api/v1/namespaces/$SNAPSHOT_NAMESPACE/configmaps/$RESULT_NAME"
if api POST "/api/v1/namespaces/$SNAPSHOT_NAMESPACE/configmaps" "$BODY" >/dev/null; then
  log "created result ConfigMap $RESULT_NAME"
else
  log "create failed (already exists?); replacing with live resourceVersion"
  RV=$(api GET "$PATH_CM" | jq -r '.metadata.resourceVersion')
  api PUT "$PATH_CM" "$(printf '%s' "$BODY" | jq --arg rv "$RV" '.metadata.resourceVersion=$rv')" >/dev/null
  log "updated result ConfigMap $RESULT_NAME"
fi

log "done"
