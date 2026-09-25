#!/usr/bin/env bash
# End-to-end fixtures for patchd.
#
# This script is the single, explicitly provided test-fixture command. It
# builds the project, runs the Go test suite, and then drives the patchd CLI
# and HTTP server through real shell invocations, checking after every
# rejected batch that the work directory is byte-for-byte unchanged.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d)"
CACHE="$(mktemp -d)"
# Pick a free loopback port (python3, then python, then a fixed fallback).
pick_port() {
  python3 - <<'PY' 2>/dev/null || python - <<'PY2' 2>/dev/null || echo 18642
import socket
s = socket.socket()
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
s.close()
PY
import socket
s = socket.socket()
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
s.close()
PY2
}
PORT="${PATCHD_PORT:-$(pick_port)}"
BASE="http://127.0.0.1:${PORT}"
PASS=0
FAIL=0

cleanup() {
  if [[ -n "${SRV_PID:-}" ]] && kill -0 "$SRV_PID" 2>/dev/null; then
    kill "$SRV_PID" 2>/dev/null || true
  fi
  rm -rf "$WORK" "$CACHE"
}
trap cleanup EXIT

ok()    { PASS=$((PASS+1)); printf 'ok   - %s\n' "$1"; }
bad()   { FAIL=$((FAIL+1)); printf 'FAIL - %s\n' "$1"; }
check() { if [[ "$1" == "$2" ]]; then ok "$3"; else bad "$3 (want [$2] got [$1])"; fi; }

# Write a quoted-heredoc JSON body (stdin) with __WORK__ replaced by the
# work directory into the file named by $1.
req() { sed "s#__WORK__#${WORK}#g" > "$1"; }

# Fingerprint a directory: sorted relpath|mode|sha256 of every file.
fp() {
  ( cd "$1" && find . -type f -print0 | LC_ALL=C sort -z | \
    xargs -0 -I{} sh -c 'printf "%s|%s|%s\n" "{}" "$(stat -c "%a" "{}")" "$(sha256sum "{}" | cut -d" " -f1)"' )
}

echo "== build =="
( cd "$ROOT" && go build -o "$WORK/patchd" ./cmd/patchd )
ok "go build"

echo "== go test =="
( cd "$ROOT" && go test ./... )
ok "go test ./..."

BIN="$WORK/patchd"

# --- Fixture workdir --------------------------------------------------------
printf 'line one\nline two\nline three\n' > "$WORK/hello.txt"
printf 'I am obsolete\n'               > "$WORK/obsolete.txt"
printf 'alpha\nbeta'                    > "$WORK/no-newline.txt"
mkdir -p "$WORK/notes"

# 1. Dry run must not change anything.
req "$WORK/req-dry.json" <<'JSON'
{"workdir":"__WORK__","dry_run":true,"patches":[{"diff":"--- a/hello.txt\n+++ b/hello.txt\n@@ -1,3 +1,3 @@\n line one\n-line two\n+line TWO\n line three\n"}]}
JSON
before="$(fp "$WORK")"
"$BIN" apply --cache-dir "$CACHE" --request "$WORK/req-dry.json" > "$WORK/out.json"
grep -q '"status": "validated"' "$WORK/out.json" && ok "dry-run reports validated"
[[ "$(fp "$WORK")" == "$before" ]] && ok "dry-run leaves directory unchanged"

# 2. Successful batch: create + modify + delete.
req "$WORK/req-batch.json" <<'JSON'
{"workdir":"__WORK__","patches":[
  {"diff":"--- /dev/null\n+++ b/notes/new.txt\n@@ -0,0 +1,2 @@\n+created by patchd\n+second line\n"},
  {"diff":"--- a/hello.txt\n+++ b/hello.txt\n@@ -1,3 +1,3 @@\n line one\n-line two\n+line TWO\n line three\n"},
  {"diff":"--- a/obsolete.txt\n+++ /dev/null\n@@ -1,1 +0,0 @@\n-I am obsolete\n"}
]}
JSON
"$BIN" apply --cache-dir "$CACHE" --request "$WORK/req-batch.json" > "$WORK/out.json"
grep -q '"status": "applied"' "$WORK/out.json" && ok "batch reports applied"
check "$(cat "$WORK/hello.txt")" "line one
line TWO
line three" "hello.txt content modified"
check "$(cat "$WORK/notes/new.txt")" "created by patchd
second line" "new file created"
[[ ! -e "$WORK/obsolete.txt" ]] && ok "file deleted"

# 3. No trailing newline preserved exactly.
req "$WORK/req-nonl.json" <<'JSON'
{"workdir":"__WORK__","patches":[{"diff":"--- a/no-newline.txt\n+++ b/no-newline.txt\n@@ -1,2 +1,2 @@\n alpha\n-beta\n\\ No newline at end of file\n+BETA\n\\ No newline at end of file\n"}]}
JSON
"$BIN" apply --cache-dir "$CACHE" --request "$WORK/req-nonl.json" > /dev/null
check "$(cat "$WORK/no-newline.txt")" "alpha
BETA" "no-newline content"
if [[ "$(tail -c1 "$WORK/no-newline.txt" | od -An -tx1 | tr -d ' ')" == "41" ]]; then
  ok "file ends with 'A' (no trailing newline)"
else
  bad "file unexpectedly ends with newline"
fi

# 4. Wrong context -> failure, directory byte-for-byte unchanged.
before="$(fp "$WORK")"
req "$WORK/req-bad.json" <<'JSON'
{"workdir":"__WORK__","patches":[{"diff":"--- a/hello.txt\n+++ b/hello.txt\n@@ -1,3 +1,3 @@\n line one\n-WRONG CONTENT\n+line TWO\n line three\n"}]}
JSON
if "$BIN" apply --cache-dir "$CACHE" --request "$WORK/req-bad.json" > "$WORK/bad.out" 2>/dev/null; then
  bad "bad-context batch should fail"
else
  ok "bad-context batch rejected (exit non-zero)"
fi
grep -q '"code": "context_mismatch"' "$WORK/bad.out" && ok "error code context_mismatch"
[[ "$(fp "$WORK")" == "$before" ]] && ok "directory unchanged after context failure"

# 5. Overlapping hunks -> failure, unchanged.
before="$(fp "$WORK")"
req "$WORK/req-overlap.json" <<'JSON'
{"workdir":"__WORK__","patches":[{"diff":"--- a/hello.txt\n+++ b/hello.txt\n@@ -1,3 +1,3 @@\n line one\n-line TWO\n+x\n line three\n@@ -3,1 +3,1 @@\n-line three\n+y\n"}]}
JSON
if "$BIN" apply --cache-dir "$CACHE" --request "$WORK/req-overlap.json" > "$WORK/ov.out" 2>/dev/null; then
  bad "overlap batch should fail"
else
  ok "overlapping hunks rejected"
fi
grep -q '"code": "overlapping_hunks"' "$WORK/ov.out" && ok "error code overlapping_hunks"
[[ "$(fp "$WORK")" == "$before" ]] && ok "directory unchanged after overlap failure"

# 6. Path traversal -> failure, unchanged.
before="$(fp "$WORK")"
req "$WORK/req-trav.json" <<'JSON'
{"workdir":"__WORK__","patches":[{"diff":"--- a/../escape.txt\n+++ b/../escape.txt\n@@ -1,0 +1,1 @@\n+evil\n"}]}
JSON
if "$BIN" apply --cache-dir "$CACHE" --request "$WORK/req-trav.json" > "$WORK/tr.out" 2>/dev/null; then
  bad "traversal batch should fail"
else
  ok "path traversal rejected"
fi
grep -q '"code": "unsafe_path"' "$WORK/tr.out" && ok "error code unsafe_path"
[[ "$(fp "$WORK")" == "$before" ]] && ok "directory unchanged after unsafe-path failure"
[[ ! -e "$(dirname "$WORK")/escape.txt" ]] && ok "no file escaped to parent"

# 7. One bad patch in a batch -> nothing at all published.
before="$(fp "$WORK")"
printf 'keep me\n' > "$WORK/second.txt"
req "$WORK/req-partial.json" <<'JSON'
{"workdir":"__WORK__","patches":[
  {"diff":"--- a/hello.txt\n+++ b/hello.txt\n@@ -1,1 +1,1 @@\n-line one\n+LINE ONE\n"},
  {"diff":"--- a/second.txt\n+++ b/second.txt\n@@ -1,1 +1,1 @@\n-WRONG\n+changed\n"}
]}
JSON
if "$BIN" apply --cache-dir "$CACHE" --request "$WORK/req-partial.json" > "$WORK/pa.out" 2>/dev/null; then
  bad "partial batch should fail"
else
  ok "batch with one bad patch rejected wholesale"
fi
[[ "$(fp "$WORK")" == "$before" ]] && ok "directory unchanged after partial-batch failure"
check "$(head -1 "$WORK/hello.txt")" "line one" "first patch was not published"

# 8. Validation failure must create no parent directories.
before_count="$(find "$WORK" -type d | wc -l)"
req "$WORK/req-failcreate.json" <<'JSON'
{"workdir":"__WORK__","patches":[
  {"diff":"--- a/deep/new.txt\n+++ b/deep/new.txt\n@@ -1,1 +1,1 @@\n-x\n+y\n"}
]}
JSON
"$BIN" apply --cache-dir "$CACHE" --request "$WORK/req-failcreate.json" > /dev/null 2>&1 || true
after_count="$(find "$WORK" -type d | wc -l)"
check "$after_count" "$before_count" "no directories created on validation failure"

# --- HTTP server ------------------------------------------------------------
if command -v curl >/dev/null 2>&1; then
  "$BIN" serve --addr "127.0.0.1:${PORT}" --cache-dir "$CACHE" >"$WORK/server.log" 2>&1 &
  SRV_PID=$!
  for _ in $(seq 1 50); do
    curl -s "$BASE/healthz" 2>/dev/null | grep -q '"ok"' && break
    sleep 0.1
  done
  curl -sf "$BASE/healthz" | grep -q '"ok"' && ok "GET /healthz"

  before="$(fp "$WORK")"
  code="$(curl -s -o "$WORK/http-bad.json" -w '%{http_code}' \
    -H 'Content-Type: application/json' \
    --data-binary @- "$BASE/v1/apply" <<JSON
{"workdir":"$WORK","patches":[{"diff":"--- a/hello.txt\n+++ b/hello.txt\n@@ -1,1 +1,1 @@\n-WRONG\n+x\n"}]}
JSON
)"
  check "$code" "422" "bad context over HTTP -> 422"
  grep -q '"code": "context_mismatch"' "$WORK/http-bad.json" && ok "HTTP error detail present"
  [[ "$(fp "$WORK")" == "$before" ]] && ok "directory unchanged after HTTP failure"

  code="$(curl -s -o "$WORK/http-ok.json" -w '%{http_code}' \
    -H 'Content-Type: application/json' \
    --data-binary @- "$BASE/v1/apply" <<JSON
{"workdir":"$WORK","patches":[{"diff":"--- a/hello.txt\n+++ b/hello.txt\n@@ -1,1 +1,1 @@\n-line one\n+LINE ONE\n"}]}
JSON
)"
  check "$code" "200" "valid patch over HTTP -> 200"
  check "$(head -1 "$WORK/hello.txt")" "LINE ONE" "HTTP patch applied"
else
  echo "skip HTTP fixtures (curl not installed)"
fi

echo
echo "=== fixtures: $PASS passed, $FAIL failed ==="
[[ "$FAIL" == 0 ]]
