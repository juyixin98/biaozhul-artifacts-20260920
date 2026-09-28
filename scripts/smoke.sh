#!/usr/bin/env bash
# End-to-end smoke check against a REAL running server (no test client).
#
# It boots uvicorn on an ephemeral port, exercises the headline semantics with
# curl, and asserts concrete JSON results with python. Exits non-zero on the
# first failed expectation, so `echo $?` is the pass/fail signal.
#
# Usage:  bash scripts/smoke.sh
set -euo pipefail

PORT="${SPM_PORT:-8099}"
BASE="http://127.0.0.1:${PORT}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"

rm -rf data logs
python3 -m uvicorn app.api:app --port "$PORT" >/tmp/spm-smoke.log 2>&1 &
SERVER_PID=$!
trap 'kill "$SERVER_PID" 2>/dev/null || true' EXIT

echo ">> waiting for server on $BASE"
for _ in $(seq 1 50); do
  curl -sf "$BASE/health" >/dev/null 2>&1 && break
  sleep 0.2
done

fail() { echo "ASSERT FAILED: $*" >&2; exit 1; }
need() { # need <description> <python-expr-over-$BODY-file>
  local desc="$1" expr="$2"
  python3 - "$BODY" "$desc" "$expr" <<'PY'
import json, sys
body_path, desc, expr = sys.argv[1], sys.argv[2], sys.argv[3]
data = json.load(open(body_path))
ok = eval(expr, {"d": data})
if not ok:
    print(f"ASSERT FAILED: {desc}", file=sys.stderr)
    print(json.dumps(data, indent=2)[:2000], file=sys.stderr)
    sys.exit(1)
print(f"ok: {desc}")
PY
}

BODY=$(mktemp)
trap 'kill "$SERVER_PID" 2>/dev/null || true; rm -f "$BODY"' EXIT

echo ">> 1. health"
curl -sf "$BASE/health" -o "$BODY"
need "health reports ok" "d['status']=='ok' and bool(d['version'])"

echo ">> 2. load pruning_trap fixture"
curl -sf -X POST "$BASE/v1/fixtures/pruning_trap" -o "$BODY"
need "fixture created" "d['sequences']==2"
CID="$(python3 -c "import json;print(json.load(open('$BODY'))['corpus_id'])")"

echo ">> 3. tight position gap (=1): trap only embeds via the LATER A"
curl -sf -X POST "$BASE/v1/mine" -H 'Content-Type: application/json' \
  -d "{\"corpus_id\":\"$CID\",\"min_support\":2,\"max_gap_position\":1}" -o "$BODY"
need "<A,B> support counts sequence identities {ok,trap}, not occurrences" \
  "next(p for p in d['patterns'] if p['pattern']==['A','B'])['support']==2"
need "<A,B> supporting identities are exactly ok and trap" \
  "set(next(p for p in d['patterns'] if p['pattern']==['A','B'])['supporting_sequence_ids'])=={'ok','trap'}"
need "trap embedding is exactly the later-A positions [2,3]" \
  "next(t for t in next(p for p in d['patterns'] if p['pattern']==['A','B'])['evidence'] if t['sequence_id']=='trap')['embeddings'][0]['positions']==[2,3]"
need "run id is returned" "d['run_id'].startswith('mine-')"

echo ">> 4. time ties: max_gap_time=0 accepts equal timestamps, rejects positive"
curl -sf -X POST "$BASE/v1/fixtures/time_ties" -o "$BODY"
TID="$(python3 -c "import json;print(json.load(open('$BODY'))['corpus_id'])")"
curl -sf -X POST "$BASE/v1/mine" -H 'Content-Type: application/json' \
  -d "{\"corpus_id\":\"$TID\",\"min_support\":1,\"max_gap_time\":0}" -o "$BODY"
need "<A,B,C> under gap 0 survives only in the triple-tied s2" \
  "next(p for p in d['patterns'] if tuple(p['pattern'])==('A','B','C'))['supporting_sequence_ids']==['s2']"
# Capture the successful run_id directly from this response (not a glob, which
# could pick up a log from the deliberately-failing requests below).
RUNID="$(python3 -c "import json;print(json.load(open('$BODY'))['run_id'])")"

echo ">> 5. failure categories are explicit, never a fake success"
code=$(curl -s -o "$BODY" -w '%{http_code}' -X POST "$BASE/v1/mine" \
  -H 'Content-Type: application/json' -d '{"corpus_id":"ghost","min_support":1}')
[ "$code" = "404" ] || fail "expected 404 got $code"
need "missing corpus -> not_found" "d['error']=='not_found'"

code=$(curl -s -o "$BODY" -w '%{http_code}' -X POST "$BASE/v1/mine" \
  -H 'Content-Type: application/json' -d "{\"corpus_id\":\"$CID\",\"min_support\":0}")
[ "$code" = "422" ] || fail "expected 422 got $code"
need "zero support -> invalid_constraint" "d['error']=='invalid_constraint'"

echo ">> 6. log file is keyed by the returned run_id and carries version+steps"
python3 - "$RUNID" <<'PY'
import json, sys
run_id = sys.argv[1]
rows = [json.loads(l) for l in open(f"logs/{run_id}.jsonl") if l.strip()]
assert all(r["run_id"] == run_id for r in rows), "run id mismatch in log"
events = [r["msg"]["event"] for r in rows]
assert {"request-received","mining-start","mining-complete","response-assembled"} <= set(events), events
start = next(r["msg"] for r in rows if r["msg"]["event"]=="mining-start")
assert start["service_version"] and start["python_version"]
print("ok: log tied to run_id with version + progress + judgement steps")
PY

echo ""
echo "ALL SMOKE ASSERTIONS PASSED (run_id=$RUNID, log=logs/$RUNID.jsonl)"
