#!/usr/bin/env bash
# End-to-end acceptance: build, generate fixtures, start the server, exercise
# the happy path and every rejection case, then shut down.
set -euo pipefail

cd "$(dirname "$0")/.."
PORT="${OCI_ACCEPT_PORT:-18097}"
BASE="http://127.0.0.1:${PORT}"
WORK="$(mktemp -d -t oci-accept-XXXXXX)"
trap 'kill "${SERVER_PID:-}" 2>/dev/null || true; rm -rf "$WORK"' EXIT

echo "== build (offline if possible) =="
cargo build --release --offline 2>/dev/null || cargo build --release

echo "== generate fixtures =="
./target/release/oci-unpack-server --help >/dev/null
cargo run --release --offline --example make_fixtures -- "$WORK" >/dev/null 2>&1 \
  || cargo run --release --example make_fixtures -- "$WORK" >/dev/null

echo "== start server on :$PORT (workdir $WORK) =="
./target/release/oci-unpack-server --port "$PORT" --workdir "$WORK" >"$WORK/server.log" 2>&1 &
SERVER_PID=$!
for _ in $(seq 1 50); do
  curl -sf "$BASE/health" >/dev/null && break
  sleep 0.1
done

fail() { echo "ACCEPTANCE FAILED: $*"; exit 1; }

echo "== happy path: demo-app =="
code=$(curl -s -o "$WORK/report.json" -w '%{http_code}' -X POST "$BASE/rebuild/demo-app")
[ "$code" = "200" ] || fail "demo-app rebuild status $code"
python3 -c 'import json;r=json.load(open("'$WORK'/report.json"));assert r["layerCount"]==3;assert r["rootfsDigest"].startswith("sha256:")' \
  || fail "unexpected report shape"

# delete+recreate content
ROOT="$WORK/roots/demo-app/$(cat "$WORK/roots/demo-app/latest")"
[ "$(cat "$ROOT/app/version")" = "3.0" ] || fail "app/version should be 3.0"
# opaque dir removed the old child
[ ! -e "$ROOT/app/logs/old.log" ] || fail "opaque dir did not mask old.log"
[ -f "$ROOT/app/logs/fresh.log" ] || fail "fresh.log missing after opaque"
# whiteout markers never materialised
find "$ROOT" -name '.wh*' | grep -q . && fail "whiteout marker landed on disk" || true
# multi-layer overwrite
[ "$(cat "$ROOT/etc/hostname")" = "demo-patched" ] || fail "overlay overwrite failed"

# determinism
d1=$(python3 -c 'import json;print(json.load(open("'$WORK'/report.json"))["rootfsDigest"])')
d2=$(curl -s -X POST "$BASE/rebuild/demo-app" | python3 -c 'import json,sys;print(json.load(sys.stdin)["rootfsDigest"])')
[ "$d1" = "$d2" ] || fail "rootfs digest not deterministic: $d1 != $d2"

# provenance + layers endpoints
code=$(curl -s -o "$WORK/f.json" -w '%{http_code}' "$BASE/rebuild/demo-app/file?path=app/version")
[ "$code" = "200" ] || fail "file lookup status $code"
python3 -c 'import json;assert json.load(open("'$WORK'/f.json"))["layerIndex"]==2' \
  || fail "app/version should come from layer 2"
curl -sf "$BASE/rebuild/demo-app/layers" | python3 -c 'import json,sys;assert len(json.load(sys.stdin)["layers"])==3'

echo "== rejection cases (expect 422, no published root) =="
for img in evil-traversal evil-symlink evil-device corrupt-layer; do
  code=$(curl -s -o "$WORK/err.json" -w '%{http_code}' -X POST "$BASE/rebuild/$img")
  echo "  $img -> $code: $(cat "$WORK/err.json")"
  [ "$code" = "422" ] || fail "$img expected 422 got $code"
  [ ! -e "$WORK/roots/$img" ] || fail "$img published a partial root"
done

echo "== misc protocol =="
[ "$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE/rebuild/ghost")" = "404" ] || fail "expected 404"
[ "$(curl -s -o /dev/null -w '%{http_code}' "$BASE/rebuild/demo-app/file?path=missing")" = "404" ] || fail "expected 404"

echo
echo "ALL ACCEPTANCE CHECKS PASSED"
