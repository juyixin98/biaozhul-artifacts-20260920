#!/usr/bin/env bash
# End-to-end demo: build two sets, run every JSON op against the CLI, and
# show the responses. Requires a built binary: cargo build --release.
set -euo pipefail

BIN="${1:-./target/release/rbitmap}"
if [[ ! -x "$BIN" ]]; then
  echo "binary not found at $BIN — run: cargo build --release" >&2
  exit 1
fi

echo "== build A = {1,2,3,70000,4294967295} (from examples/build.json) =="
RESP_A=$("$BIN" examples/build.json)
echo "$RESP_A"
SET_A=$(printf '%s' "$RESP_A" | sed -n 's/.*"set":"\([^"]*\)".*/\1/p')

echo
echo "== build B = {3,4,5,65536,4294967295} =="
RESP_B=$(echo '{"op":"build","values":[3,4,5,65536,4294967295]}' | "$BIN")
echo "$RESP_B"
SET_B=$(printf '%s' "$RESP_B" | sed -n 's/.*"set":"\([^"]*\)".*/\1/p')

echo
echo "== union A B =="
echo "{\"op\":\"union\",\"set\":\"$SET_A\",\"other\":\"$SET_B\"}" | "$BIN"
echo
echo "== intersect A B =="
echo "{\"op\":\"intersect\",\"set\":\"$SET_A\",\"other\":\"$SET_B\"}" | "$BIN"
echo
echo "== difference A B =="
echo "{\"op\":\"difference\",\"set\":\"$SET_A\",\"other\":\"$SET_B\"}" | "$BIN"
echo
echo "== decode A (lists values) =="
echo "{\"op\":\"decode\",\"set\":\"$SET_A\"}" | "$BIN"
echo
echo "== contains 70000 in A (expect true) and 70001 (expect false) =="
echo "{\"op\":\"contains\",\"set\":\"$SET_A\",\"value\":70000}" | "$BIN"
echo "{\"op\":\"contains\",\"set\":\"$SET_A\",\"value\":70001}" | "$BIN"
echo
echo "== insert 999 into A, then remove it =="
INS=$(echo "{\"op\":\"insert\",\"set\":\"$SET_A\",\"value\":999}" | "$BIN")
echo "$INS"
SET_A2=$(printf '%s' "$INS" | sed -n 's/.*"set":"\([^"]*\)".*/\1/p')
echo "{\"op\":\"remove\",\"set\":\"$SET_A2\",\"value\":999}" | "$BIN"
echo
echo "== hostile length is rejected, not crashed =="
# RBM1 v1 header + varint(70000) container count — exceeds the default
# max_containers limit of 65536, so decoding must fail fast.
HOSTILE=$(printf 'RBM1\x01\x00\xf0\xa2\x04' | base64 -w0)
echo "{\"op\":\"decode\",\"set\":\"$HOSTILE\"}" | "$BIN" || true
