#!/usr/bin/env bash
# Live end-to-end walkthrough against a running bis server.
# Only the fixture commands declared in examples/*/build.json are executed;
# this script only drives the JSON API.
set -u
BASE=${BASE:-http://127.0.0.1:18091}
OUT=${OUT:-logs/live}
REPO=$(cd "$(dirname "$0")/.." && pwd)
mkdir -p "$OUT"

req() { # method path [json-body]
  local method=$1 path=$2 body=${3:-}
  if [ -n "$body" ]; then
    curl -sS -X "$method" -H 'Content-Type: application/json' -d "$body" "$BASE$path"
  else
    curl -sS -X "$method" "$BASE$path"
  fi
}

echo "== 1. import deterministic diamond project =="
req PUT /projects/shared-demo "{\"src_dir\":\"$REPO/examples/shared-demo\"}" | tee "$OUT/01-import.json"; echo

echo "== 2. build (all three fixture commands should execute) =="
req POST /projects/shared-demo/build | tee "$OUT/02-build.json"; echo

echo "== 3. build again (cache hits: zero fixture commands) =="
req POST /projects/shared-demo/build | tee "$OUT/03-build-cached.json"; echo

echo "== 4. verify (complete chain) =="
req GET /projects/shared-demo/verify | tee "$OUT/04-verify-ok.json"; echo

echo "== 5. impact of the SHARED source (compile_a, compile_b, link) =="
req GET "/projects/shared-demo/impact?path=src/shared.txt" | tee "$OUT/05-impact-shared.json"; echo

echo "== 6. impact of branch source a.txt (compile_a, link; NOT compile_b) =="
req GET "/projects/shared-demo/impact?path=src/a.txt" | tee "$OUT/06-impact-branch.json"; echo

echo "== 7. fetch signed provenance record of the link action =="
req GET /projects/shared-demo/records/link | tee "$OUT/07-record-link.json"; echo

echo "== 8. reproduce in an isolated store (byte-identical -> reproduced=true) =="
req POST /projects/shared-demo/reproduce | tee "$OUT/08-reproduce-ok.json"; echo

echo "== 9. import + build the NON-deterministic project =="
req PUT /projects/nondet-demo "{\"src_dir\":\"$REPO/examples/nondet-demo\"}" | tee "$OUT/09-import-nondet.json"; echo
req POST /projects/nondet-demo/build | tee "$OUT/10-build-nondet.json"; echo

echo "== 10. nondet: provenance complete but NOT reproducible (HTTP 409) =="
req POST /projects/nondet-demo/reproduce | tee "$OUT/11-reproduce-nondet.json"; echo

echo "== 11. declarative graph view =="
req GET /projects/shared-demo/graph | tee "$OUT/12-graph.json"; echo

echo "DONE"
