#!/usr/bin/env bash
# End-to-end HTTP examples. Start the service first:
#   python3 -m resflow.cli serve --port 8080
set -u
HOST=${RESFLOW_HOST:-127.0.0.1}
PORT=${RESFLOW_PORT:-8080}
BASE="http://${HOST}:${PORT}"

say() { printf '\n=== %s ===\n' "$1"; }

say "health"
curl -s "${BASE}/health"; echo

say "double release + use-after-release (200 with findings)"
curl -s -X POST "${BASE}/analyze" -H 'Content-Type: application/json' \
  --data @samples/request_double_release.json \
  | python3 -c '
import sys, json
d = json.load(sys.stdin)
print("counts:", json.dumps(d["counts"]))
for f in d["findings"]:
    s = f["span"]["start"]
    print("#%s %s %s:%s %s" % (f["id"], f["code"], s["line"], s["column"], f["message"]))
'

say "partial initialization across a branch"
curl -s -X POST "${BASE}/analyze" -H 'Content-Type: application/json' \
  --data @samples/request_partial_init.json \
  | python3 -c 'import sys,json; d=json.load(sys.stdin); print("paths:", d["counts"]["paths"], "findings:", sorted(f["code"] for f in d["findings"]))'

say "exceptional exit leaks the held resource"
curl -s -X POST "${BASE}/analyze" -H 'Content-Type: application/json' \
  --data @samples/request_exception_exit.json \
  | python3 -c 'import sys,json; d=json.load(sys.stdin); print("paths:", [(p["path_id"], p["kind"], p["finding_ids"]) for f in d["functions"] for p in f["paths"]])'

say "syntax error (400 E-PARSE with span)"
curl -s -o - -w '\nHTTP %{http_code}\n' -X POST "${BASE}/analyze" \
  -H 'Content-Type: application/json' --data @samples/request_bad_syntax.json
