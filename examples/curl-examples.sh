#!/usr/bin/env bash
# End-to-end request examples for the reprobuild HTTP API.
# Start the server first:
#   ./bin/reprobuild serve -addr 127.0.0.1:18391 -state-dir ./.reprobuild
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:18391}"

echo "== health =="
curl -s "$BASE/api/v1/healthz"; echo

echo "== create a build from inline files + fixtures =="
RESP=$(curl -s -X POST "$BASE/api/v1/builds" \
  -H 'Content-Type: application/json' \
  --data @examples/build-with-fixtures.json)
echo "$RESP"
ID=$(printf '%s' "$RESP" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
SHA=$(printf '%s' "$RESP" | python3 -c 'import json,sys; print(json.load(sys.stdin)["artifact"]["sha256"])')

echo "== fetch the build record =="
curl -s "$BASE/api/v1/builds/$ID"; echo

echo "== download the artifact and verify sha256 =="
curl -s "$BASE/api/v1/builds/$ID/artifact" -o /tmp/artifact.tar
echo "server sha: $SHA"
echo "local  sha: $(sha256sum /tmp/artifact.tar | cut -d' ' -f1)"

echo "== list the deterministic tar =="
tar tvf /tmp/artifact.tar

echo "== escaping-symlink build is rejected (status=error) =="
curl -s -X POST "$BASE/api/v1/builds" \
  -H 'Content-Type: application/json' \
  --data @examples/build-rejected-escape.json; echo

echo "== one-shot pack of an existing directory =="
./bin/reprobuild pack -src ./.reprobuild -o /tmp/snapshot.tar || true
