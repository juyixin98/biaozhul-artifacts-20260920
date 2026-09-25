#!/usr/bin/env bash
# End-to-end request examples against the running JSON service.
# Usage: ./curl-examples.sh [base-url]   (default http://127.0.0.1:8080)
#
# The service must already be running, e.g.:
#   java -jar target/segment-merge-index-1.0.0.jar --dir /tmp/ivx-demo --port 8080 --docs 60
set -u
BASE="${1:-http://127.0.0.1:8080}"
j() { python3 -m json.tool 2>/dev/null || cat; }

echo "== 1. add a document (auto id) =="
curl -s -X POST "$BASE/documents" -H 'Content-Type: application/json' \
  -d '{"text":"inverted index segment merge demo"}' | j

echo; echo "== 2. add with an explicit id =="
curl -s -X POST "$BASE/documents" -H 'Content-Type: application/json' \
  -d '{"id":500,"text":"segment merge policy note"}' | j

echo; echo "== 3. update same id -> new generation =="
curl -s -X POST "$BASE/documents" -H 'Content-Type: application/json' \
  -d '{"id":500,"text":"segment merge policy revised"}' | j

echo; echo "== 4. fetch the current revision =="
curl -s "$BASE/documents/500" | j

echo; echo "== 5. term query =="
curl -s "$BASE/search?q=segment" | j

echo; echo "== 6. boolean AND query (POST) =="
curl -s -X POST "$BASE/search" -H 'Content-Type: application/json' \
  -d '{"q":"AND:segment,merge"}' | j

echo; echo "== 7. boolean OR query =="
curl -s "$BASE/search?q=OR:cafe,rocket" | j

echo; echo "== 8. force a flush (RAM buffer -> segment) =="
curl -s -X POST "$BASE/flush" | j

echo; echo "== 9. inspect stats before merge =="
curl -s "$BASE/stats" | j

echo; echo "== 10. merge all segments =="
curl -s -X POST "$BASE/merge" | j

echo; echo "== 11. stats after merge =="
curl -s "$BASE/stats" | j

echo; echo "== 12. delete the document =="
curl -s -X DELETE "$BASE/documents/500" | j

echo; echo "== 13. confirm gone (404) =="
curl -s -i "$BASE/documents/500" | head -1

echo; echo "== 14. old text no longer matches =="
curl -s "$BASE/search?q=revised" | j
