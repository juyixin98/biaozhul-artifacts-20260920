#!/usr/bin/env bash
# End-to-end acceptance demo over HTTP (requires curl + a running server).
#
# Reproduces the required scenario:
#   1. a long read transaction spans three overwrites and a delete; its old
#      view never changes;
#   2. two concurrent writers touch the same key -> exactly one commits;
#   3. versions pinned by an open snapshot cannot be GC'd; after the snapshot
#      closes, GC reclaims them and every read result stays consistent.
#
# Usage:
#   ./examples/demo.sh            # talks to http://127.0.0.1:3000
#   BASE=http://host:port ./examples/demo.sh
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:3000}"

# pretty JSON if python3 is available, otherwise print raw
pretty() {
  if command -v python3 >/dev/null 2>&1; then python3 -m json.tool; else cat; fi
}

req() { # method path [body]
  local method="$1" path="$2" body="${3:-}"
  echo ">>> $method $path ${body:+<$body>}"
  if [ -n "$body" ]; then
    curl -sS -X "$method" -H 'content-type: application/json' -d "$body" "$BASE$path" | pretty
  else
    curl -sS -X "$method" "$BASE$path" | pretty
  fi
}

json_field() { # field  (reads stdin)
  if command -v python3 >/dev/null 2>&1; then
    python3 -c "import sys,json; print(json.load(sys.stdin)['$1'])"
  else
    grep -o "\"$1\"[[:space:]]*:[[:space:]]*[0-9]\+" | grep -o '[0-9]\+' | head -1
  fi
}

echo "=== 0. health / endpoint map ==="
req GET /

echo "=== 1. seed k=\"v0\" (commit ts=1) ==="
req PUT /kv/k '"v0"'

echo "=== 2. open the LONG read transaction at ts=1 ==="
LONG=$(curl -sS -X POST "$BASE/txn/begin" | tee /dev/stderr | json_field txn_id)
echo "(long txn id = $LONG)" 2>/dev/null

echo "=== 3. open an explicit read-only snapshot at ts=1 ==="
S1=$(curl -sS -X POST -H 'content-type: application/json' -d '{"ts":1}' "$BASE/snapshot/open" | tee /dev/stderr | json_field snapshot_id)
echo "(snapshot id = $S1)" 2>/dev/null

echo "=== 4. three overwrites v1,v2,v3 and a delete (commit ts=2..5) ==="
req PUT /kv/k '"v1"'
req PUT /kv/k '"v2"'
req PUT /kv/k '"v3"'
req DELETE /kv/k

echo "=== 5. old readers still see v0; a new reader sees deleted ==="
req GET "/txn/$LONG/kv/k"
req GET "/snapshot/$S1/kv/k"
req GET /kv/k
req GET '/kv/k?as_of=4'

echo "=== 6. version chain: all 5 versions present ==="
req GET /debug/versions/k

echo "=== 7. GC with readers open: watermark pinned at 1, nothing removed ==="
req POST /gc

echo "=== 8. close the ts1 snapshot; long txn still pins the watermark ==="
req POST "/snapshot/$S1/close"
req POST /gc

echo "=== 9. record the post-delete view in a snapshot at ts=5 ==="
S5=$(curl -sS -X POST -H 'content-type: application/json' -d '{"ts":5}' "$BASE/snapshot/open" | tee /dev/stderr | json_field snapshot_id)
echo "(snapshot id = $S5)" 2>/dev/null

echo "=== 10. abort the long reader; GC reclaims v0..v3, tombstone@5 stays pinned by the snapshot ==="
req POST "/txn/$LONG/abort"
req POST /gc
req GET /debug/versions/k

echo "=== 11. reads are identical before/after reclamation ==="
req GET "/snapshot/$S5/kv/k"
req GET /kv/k
req GET '/kv/k?as_of=5'

echo "=== 12. close the snapshot; the tombstone-only chain is now fully reclaimed ==="
req POST "/snapshot/$S5/close"
req POST /gc
req GET /debug/versions/k
req GET /kv/k

echo "=== 13. concurrent writers on a fresh key: tA and tB buffer writes, both try to commit ==="
TA=$(curl -sS -X POST "$BASE/txn/begin" | json_field txn_id)
TB=$(curl -sS -X POST "$BASE/txn/begin" | json_field txn_id)
req PUT "/txn/$TA/kv/c" '"A-wins"'
req PUT "/txn/$TB/kv/c" '"B-loses"'
req POST "/txn/$TA/commit"
echo "--- the second commit must fail with HTTP 409 write_conflict:"
req POST "/txn/$TB/commit" || true
req GET /kv/c
req GET /stats

echo "=== demo finished ==="
