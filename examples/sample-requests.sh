#!/usr/bin/env bash
# End-to-end request samples for the conditional-update service.
# Starts from a fresh server:  go run ./cmd/server -addr 127.0.0.1:8080
#
# Run:  bash examples/sample-requests.sh
set -u
BASE="${BASE:-http://127.0.0.1:8080}"
key="sample-doc"

hdr() { printf '\n----- %s -----\n' "$1"; }

hdr "Create the resource (201, note the strong ETag)"
curl -s -i -X POST "$BASE/resources/$key" --data-binary 'draft one'
echo

hdr "Read it back and capture the ETag"
ETAG=$(curl -s -D - -o /tmp/sample-body "$BASE/resources/$key" \
  | tr -d '\r' | awk 'tolower($1)=="etag:"{print $2}')
echo "ETag=$ETAG  body=$(cat /tmp/sample-body)"

hdr "Update WITH the matching ETag (200, new ETag returned)"
curl -s -i -X PUT "$BASE/resources/$key" -H "If-Match: $ETAG" --data-binary 'draft two'
echo

hdr "Update with the SAME (now stale) ETag (412 Precondition Failed)"
curl -s -i -X PUT "$BASE/resources/$key" -H "If-Match: $ETAG" --data-binary 'late writer'
echo

hdr "Update WITHOUT any precondition (428 Precondition Required)"
curl -s -i -X PUT "$BASE/resources/$key" --data-binary 'no precondition'
echo

hdr "Update with a WEAK validator W/... (412, weak cannot strong-compare)"
curl -s -i -X PUT "$BASE/resources/$key" -H "If-Match: W/$ETAG" --data-binary 'weak try'
echo

hdr "Wildcard update If-Match: * (200 while a representation exists)"
curl -s -i -X PUT "$BASE/resources/$key" -H 'If-Match: *' --data-binary 'wildcard write'
echo

hdr "Delete with If-Match: * (204)"
curl -s -i -X DELETE "$BASE/resources/$key" -H 'If-Match: *'
echo

hdr "Wildcard update after delete (412, no current representation)"
curl -s -i -X PUT "$BASE/resources/$key" -H 'If-Match: *' --data-binary 'ghost write'
echo

hdr "Inspect the in-process fake audit log"
curl -s "$BASE/audit"; echo
