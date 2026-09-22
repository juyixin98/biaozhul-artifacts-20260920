#!/usr/bin/env bash
# End-to-end acceptance for the fork ledger indexer.
#
# Exercises, against a real PostgreSQL and the real server binary:
#   1. orphan staging  (block delivered before its parent)
#   2. equal-weight hash tie-break, then a heavier branch forcing a reorg
#   3. duplicate delivery is idempotent (no double counting)
#   4. same-hash/different-content is rejected
#   5. balances, head, chain, account transactions over HTTP
#   6. file ingest + crash/restart from the durable cursor (3 runs)
#   7. incremental result == from-genesis rebuild
#
# Exit code is nonzero on the first failed expectation.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

DATABASE_URL="${DATABASE_URL:-host=/var/run/postgresql user=admin dbname=forkindexer}"
HTTP_HOST="127.0.0.1"
PORT="$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()')"
BASE="http://$HTTP_HOST:$PORT"
export DATABASE_URL

say()  { printf '\n\033[36m▶ %s\033[0m\n' "$*"; }
fail() { printf '\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

# jq-free JSON field extractor (string or number).
jget() { python3 -c 'import json,sys;d=json.load(sys.stdin);print(d[sys.argv[1]])' "$1"; }

say "build binaries"
make build >/dev/null
make fixtures >/dev/null
mapfile -t EXP < <(python3 -c '
import json
d=json.load(open("testdata/expected.json"))
print(d["head"]); print(d["balances"]["0xaa00000000000000000000000000000000000001"])
print(d["balances"]["0xbb00000000000000000000000000000000000002"]); print(d["balances"]["0xcc00000000000000000000000000000000000003"])')
WANT_HEAD="${EXP[0]}"; WANT_A="${EXP[1]}"; WANT_B="${EXP[2]}"; WANT_C="${EXP[3]}"

say "reset database"
./bin/indexer reset >/dev/null

say "start server on $BASE"
HTTP_ADDR="$HTTP_HOST:$PORT" DATABASE_URL="$DATABASE_URL" ./bin/forkindexerd &>/tmp/forkindexerd.acceptance.log &
SRV=$!
trap 'kill $SRV 2>/dev/null || true' EXIT
for _ in $(seq 1 50); do
  curl -fsS --noproxy '*' "$BASE/healthz" >/dev/null 2>&1 && break
  sleep 0.1
done

post() { # post <file> [ctype]
  local ct="${2:-application/json}"
  curl -fsS --noproxy '*' -X POST -H "Content-Type: $ct" --data-binary @"$1" "$BASE/v1/blocks"
}

say "deliver A1 before genesis -> must be staged, no head"
head -1 testdata/stream.ndjson > /tmp/a1.ndjson
post /tmp/a1.ndjson application/x-ndjson | tee /tmp/r1.json
[ "$(python3 -c 'import json;print(json.load(open("/tmp/r1.json"))["blocks"][0]["status"])')" = staged ] || fail "A1 not staged"
curl -fsS --noproxy '*' "$BASE/v1/head" | tee /tmp/head0.json
[ "$(python3 -c 'import json;print(json.load(open("/tmp/head0.json"))["canonical"])')" = False ] || fail "head exists while only orphan stored"

say "deliver remaining blocks G,B1,A2 -> reorg to final chain G-A1-A2"
tail -n +2 testdata/stream.ndjson > /tmp/rest.ndjson
post /tmp/rest.ndjson application/x-ndjson > /tmp/r2.json
curl -fsS --noproxy '*' "$BASE/v1/head" | tee /tmp/head.json
[ "$(jget hash < /tmp/head.json)" = "$WANT_HEAD" ] || fail "head mismatch"
[ "$(jget height < /tmp/head.json)" = 2 ] || fail "head height != 2"

say "balances: Alice=$WANT_A Bob=$WANT_B Carol=$WANT_C"
bal() { curl -fsS --noproxy '*' "$BASE/v1/accounts/$1/balance" | jget balance; }
[ "$(bal 0xaa00000000000000000000000000000000000001)" = "$WANT_A" ] || fail "alice balance"
[ "$(bal 0xbb00000000000000000000000000000000000002)" = "$WANT_B" ] || fail "bob balance"
[ "$(bal 0xcc00000000000000000000000000000000000003)" = "$WANT_C" ] || fail "carol balance"

say "canonical chain is G -> A1 -> A2 (depth 3, correct order)"
curl -fsS --noproxy '*' "$BASE/v1/chain" | tee /tmp/chain.json
[ "$(jget depth < /tmp/chain.json)" = 3 ] || fail "chain depth"
python3 - <<PY
import json
d=json.load(open("/tmp/chain.json"))
want=json.load(open("testdata/expected.json"))["canonicalChain"]
got=[b["hash"] for b in d["blocks"]][::-1]  # API returns tip -> genesis
assert got==want,(got,want)
PY

say "account transactions for Bob indexed on canonical chain"
curl -fsS --noproxy '*' "$BASE/v1/accounts/0xbb00000000000000000000000000000000000002/transactions" \
  | tee /tmp/txs.json
[ "$(python3 -c 'import json;print(len(json.load(open("/tmp/txs.json"))["transactions"]))')" = 2 ] \
  || fail "bob should have 2 canonical transfers"

say "re-deliver whole stream -> all duplicates, balances unchanged"
post testdata/stream.ndjson application/x-ndjson | tee /tmp/dup.json
[ "$(jget accepted < /tmp/dup.json)" = 0 ] || fail "duplicate accepted new blocks"
[ "$(jget duplicates < /tmp/dup.json)" = 4 ] || fail "expected 4 duplicates"
[ "$(bal 0xaa00000000000000000000000000000000000001)" = "$WANT_A" ] || fail "balance changed on redelivery"

say "same hash, different content -> HTTP 400"
code=$(curl -s -o /tmp/tampered.out -w '%{http_code}' --noproxy '*' -X POST \
  -H 'Content-Type: application/json' --data-binary @testdata/tampered_hash.json "$BASE/v1/blocks")
[ "$code" = 400 ] || fail "tampered block accepted with $code"
grep -q 'does not match SHA-256' /tmp/tampered.out || fail "tampered error message"

say "independent from-genesis rebuild matches incremental state"
code=$(curl -s -o /tmp/verify.out -w '%{http_code}' --noproxy '*' -X POST "$BASE/v1/verify/rebuild")
[ "$code" = 200 ] || fail "verify HTTP $code: $(cat /tmp/verify.out)"
[ "$(jget match < /tmp/verify.out)" = True ] || fail "rebuild mismatch: $(cat /tmp/verify.out)"

say "crash/restart: kill server, then file ingest with two mid-stream crashes"
kill $SRV; wait $SRV 2>/dev/null || true
trap - EXIT
# Fresh 61-block stream so crash boundaries are meaningful.
./bin/indexer reset >/dev/null
go run ./cmd/genlongstream > /tmp/crashstream.ndjson
TOTAL=$(wc -l < /tmp/crashstream.ndjson)
./bin/indexer ingest-file --file /tmp/crashstream.ndjson --batch-size 7 --exit-before-batch 2 >/dev/null || true
seqof() { DATABASE_URL="$DATABASE_URL" ./bin/indexer state 2>/dev/null | python3 -c 'import json,sys;print(json.load(sys.stdin)["ingestSeq"])'; }
S1=$(seqof)
[ "$S1" = 7 ] || fail "crash1 seq=$S1 want 7"
./bin/indexer ingest-file --file /tmp/crashstream.ndjson --batch-size 7 --exit-before-batch 5 >/dev/null || true
S2=$(seqof)
[ "$S2" = 28 ] || fail "crash2 seq=$S2 want 28"
./bin/indexer ingest-file --file /tmp/crashstream.ndjson --batch-size 7 >/dev/null
S3=$(seqof)
[ "$S3" = "$TOTAL" ] || fail "final seq=$S3 want $TOTAL"

# Duplicate replay must not change anything, then rebuild must match.
./bin/indexer ingest-file --file /tmp/crashstream.ndjson --batch-size 7 >/dev/null
S4=$(seqof)
[ "$S4" = "$TOTAL" ] || fail "dup replay seq=$S4"
./bin/indexer verify | tee /tmp/verify2.json | grep -q '"match": true' || fail "post-crash rebuild mismatch"

say "restore demo fixture state for the README quickstart"
./bin/indexer reset >/dev/null
./bin/indexer ingest-file --file testdata/stream.ndjson >/dev/null
./bin/indexer verify | grep -q '"match": true' || fail "fixture rebuild mismatch"

printf '\n\033[32m✓ acceptance passed\033[0m\n'
