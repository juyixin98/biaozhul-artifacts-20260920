#!/usr/bin/env bash
# Real crash-recovery acceptance test: starts the server, uploads a partial
# object, sends SIGKILL (no graceful shutdown), restarts, resumes, completes,
# kills again, restarts, and finally verifies only the complete hash-correct
# object is readable.
#
# Usage: ./examples/crash_recovery.sh [path-to-binary]   (default ./target/debug/chunk-upload)
# Requires: python3, curl, sha256sum, coreutils (kill)
set -euo pipefail

BIN="${1:-./target/debug/chunk-upload}"
PORT="${PORT:-18093}"
BASE="http://127.0.0.1:$PORT"
W="$(mktemp -d)"; DATA="$(mktemp -d)"
trap 'kill -9 $SRVPID 2>/dev/null; rm -rf "$W" "$DATA"' EXIT

export RUSTUP_HOME="${RUSTUP_HOME:-$HOME/.rustup}"
export CARGO_HOME="${CARGO_HOME:-$HOME/.cargo}"

start() { "$BIN" --addr "127.0.0.1:$PORT" --data-dir "$DATA" >"$W/srv.log" 2>&1 & SRVPID=$!; }
kill_hard() { kill -9 "$SRVPID"; wait "$SRVPID" 2>/dev/null || true; }

start; sleep 1
python3 - "$BASE" "$W" <<'PY'
import hashlib, json, os, sys, urllib.request
BASE, W = sys.argv[1], sys.argv[2]; CS = 1024 * 1024
data = os.urandom(3 * CS + 123)          # 4 parts; last part is 123 bytes
open(f"{W}/o.bin", "wb").write(data)
h = hashlib.sha256(data).hexdigest()
req = urllib.request.Request(BASE + "/uploads",
    data=json.dumps({"key": "k", "total_size": len(data), "sha256": h, "chunk_size": CS}).encode(),
    headers={"content-type": "application/json"}, method="POST")
sid = json.load(urllib.request.urlopen(req))["upload_id"]
open(f"{W}/id", "w").write(sid)
for n in (0, 2):                          # leave parts 1 and 3 missing
    b = data[n * CS:min((n + 1) * CS, len(data))]
    urllib.request.urlopen(urllib.request.Request(f"{BASE}/uploads/{sid}/parts/{n}", data=b, method="PUT")).read()
print("session created; parts 0 and 2 uploaded; 1 and 3 missing")
PY

echo "--- SIGKILL server mid-upload ---"; kill_hard
echo "--- restart, verify state survived and object still unreadable ---"; start; sleep 1
ID="$(cat "$W/id")"
curl -sS "$BASE/uploads/$ID" | python3 -c 'import sys,json;d=json.load(sys.stdin);assert d["received_parts"]==[0,2] and d["missing_parts"]==[1,3];print("state OK: received",d["received_parts"],"missing",d["missing_parts"])'
curl -sS -o /dev/null -w 'object before completion: HTTP %{http_code} (want 404)\n' "$BASE/objects/k"

python3 - "$BASE" "$W" <<'PY'
import json, os, sys, urllib.request
BASE, W = sys.argv[1], sys.argv[2]; CS = 1024 * 1024
sid = open(f"{W}/id").read(); data = open(f"{W}/o.bin", "rb").read()
for n in (1, 3):                          # resume the missing parts
    b = data[n * CS:min((n + 1) * CS, len(data))]
    urllib.request.urlopen(urllib.request.Request(f"{BASE}/uploads/{sid}/parts/{n}", data=b, method="PUT")).read()
code = urllib.request.urlopen(urllib.request.Request(f"{BASE}/uploads/{sid}/complete", data=b"", method="POST")).status
assert code == 201, code
print("resumed + completed: HTTP", code)
PY

echo "--- SIGKILL after publish, then restart ---"; kill_hard; start; sleep 1
curl -sS -o "$W/got" -w 'read after crash: HTTP %{http_code}\n' "$BASE/objects/k"
curl -sS -o /dev/null -w 'idempotent re-complete after restart: HTTP %{http_code}\n' -X POST "$BASE/uploads/$ID/complete"
python3 - "$W" <<'PY'
import hashlib, sys
W = sys.argv[1]
a = open(f"{W}/o.bin", "rb").read(); b = open(f"{W}/got", "rb").read()
assert a == b, "bytes differ"
assert hashlib.sha256(b).hexdigest() == hashlib.sha256(a).hexdigest()
print(f"VERIFIED: {len(b)} bytes identical, sha256 correct")
PY
echo "CRASH-RECOVERY ACCEPTANCE PASSED"
