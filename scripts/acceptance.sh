#!/usr/bin/env bash
# End-to-end acceptance walkthrough against a real running server over TCP.
#
# Deterministically interleaves two writers with a long-lived reader and
# demonstrates: fixed-snapshot read consistency, write-write conflict
# (HTTP 409), GC retaining versions while a snapshot is pinned, and space
# reclamation after the snapshot is released.
#
# Usage: ./scripts/acceptance.sh [addr]
set -u

ADDR="${1:-127.0.0.1:8080}"
BASE="http://$ADDR"

say()  { printf '\n=== %s ===\n' "$*"; }
raw()  { # method path [json-body]
  if [ -n "${3:-}" ]; then
    curl -sS -X "$1" "$BASE$2" -H 'content-type: application/json' -d "$3"
  else
    curl -sS -X "$1" "$BASE$2"
  fi
}
num()  { # extract integer field $1 from stdin (no jq dependency)
  sed "s/.*\"$1\":\([0-9][0-9]*\).*/\1/"
}
call() { # method path [json-body]  -> prints body
  local out
  out="$(raw "$@")"
  printf '%s\n' "$out"
}
begin()  { raw POST /tx/begin | num tx; }
check() { # label expected_substring actual
  case "$3" in
    *"$2"*) ;;
    *) echo "ASSERT FAILED: $1 (expected to contain: $2)"; return 1 ;;
  esac
  echo "ok: $1"
}

say "health"
call GET /health

say "writer 0: PUT k=base -> commit v1"
TX0="$(begin)"
call POST "/tx/$TX0/put" '{"key":"k","value":"base"}'
check "commit v1" '"version":1' "$(call POST "/tx/$TX0/commit")"

say "long reader: pin a snapshot at v1"
SNAP_OUT="$(raw POST /snapshot)"
echo "$SNAP_OUT"
SNAP="$(printf '%s' "$SNAP_OUT" | num snapshot)"
check "snapshot at v1" '"version":1' "$SNAP_OUT"

say "writers A and B both open at v1 and write k"
WA="$(begin)"
WB="$(begin)"
call POST "/tx/$WA/put" '{"key":"k","value":"A"}'
call POST "/tx/$WB/put" '{"key":"k","value":"B"}'

say "A commits first -> v2"
check "commit v2" '"version":2' "$(call POST "/tx/$WA/commit")"

say "B commits -> expected 409 conflict"
CONFLICT_OUT="$(raw POST "/tx/$WB/commit")"
CONFLICT_STATUS="$(curl -sS -o /dev/null -w '%{http_code}' -X POST "$BASE/tx/$WB/commit")"
echo "$CONFLICT_OUT"
check "conflict body" 'write-write conflict' "$CONFLICT_OUT"
[ "$CONFLICT_STATUS" = "409" ] && echo "ok: HTTP 409" || { echo "ASSERT FAILED: status $CONFLICT_STATUS != 409"; exit 1; }

say "abort loser B, retry from a fresh snapshot -> v3"
call POST "/tx/$WB/abort" >/dev/null
WB2="$(begin)"
call POST "/tx/$WB2/put" '{"key":"k","value":"B"}'
check "commit v3" '"version":3' "$(call POST "/tx/$WB2/commit")"

say "pinned snapshot still reads base; latest reads B"
SNAP_READ="$(raw POST "/snapshot/$SNAP/get" '{"key":"k"}')"
echo "$SNAP_READ"
check "snapshot reads base" '"value":"base"' "$SNAP_READ"
T="$(begin)"
check "latest reads B" '"value":"B"' "$(raw POST "/tx/$T/get" '{"key":"k"}')"
call POST "/tx/$T/abort" >/dev/null

say "GC while pinned: horizon stays 1; seg-1 folds into base, seg-2/3 retained"
call GET /stats
GC1="$(raw POST /gc)"
echo "$GC1"
check "gc horizon pinned at 1" '"horizon":1' "$GC1"
STATS1="$(raw GET /stats)"
echo "$STATS1"
check "2 segments retained (v2,v3)" '"segments":2' "$STATS1"
SNAP_READ2="$(raw POST "/snapshot/$SNAP/get" '{"key":"k"}')"
check "snapshot still base after gc" '"value":"base"' "$SNAP_READ2"

say "release snapshot"
call POST "/snapshot/$SNAP/release"

say "GC after release: one base, segments removed, bytes reclaimed"
GC2="$(raw POST /gc)"
echo "$GC2"
check "gc horizon advances to 3" '"horizon":3' "$GC2"
check "files removed" '"files_removed":3' "$GC2"
STATS2="$(raw GET /stats)"
echo "$STATS2"
check "no segments remain" '"segments":0' "$STATS2"
check "one base" '"bases":1' "$STATS2"

say "latest value after reclamation"
T="$(begin)"
check "latest still B" '"value":"B"' "$(raw POST "/tx/$T/get" '{"key":"k"}')"
call POST "/tx/$T/abort" >/dev/null

say "fault injection: arm an fsync failure; commit rejected then retried"
call POST /admin/faults '{"op":"sync","leave_written":false}'
T="$(begin)"
call POST "/tx/$T/put" '{"key":"f","value":"1"}'
FAULT_OUT="$(raw POST "/tx/$T/commit")"
echo "$FAULT_OUT"
check "injected sync error surfaced" 'injected fault' "$FAULT_OUT"
check "retry commits v4" '"version":4' "$(call POST "/tx/$T/commit")"
call POST /admin/faults/reset >/dev/null

echo
echo "acceptance walkthrough complete"
