#!/usr/bin/env bash
# End-to-end acceptance: build, start the server, rebuild every fixture, and
# assert the expected HTTP status / error code / no-partial-publish guarantee.
set -euo pipefail

cd "$(dirname "$0")/.."

PORT="${OCI_TEST_PORT:-18097}"
BASE="http://127.0.0.1:${PORT}"
WORK="$(mktemp -d)"
FX="$WORK/fixtures"
BD="$WORK/builds"
LOG="$WORK/server.log"
trap 'kill "${SRV_PID:-0}" 2>/dev/null || true; rm -rf "$WORK"' EXIT

echo "== build =="
cargo build --release
FIX=./target/release/fixture-maker
SRV=./target/release/oci-unpack-server

echo "== generate fixtures -> $FX =="
"$FIX" "$FX" >/dev/null

echo "== start server on $PORT =="
OCI_FIXTURES_DIR="$FX" OCI_BUILDS_DIR="$BD" OCI_BIND="127.0.0.1:${PORT}" \
  "$SRV" >"$LOG" 2>&1 &
SRV_PID=$!

# Wait for readiness.
for _ in $(seq 1 50); do
  if curl -sf "$BASE/healthz" >/dev/null 2>&1; then break; fi
  sleep 0.1
done
curl -sf "$BASE/healthz" >/dev/null

fail=0
check() { # name expected_code expected_code_substr url
  local name="$1" want_code="$2" want_code_field="$3" url="$4"
  local resp code field
  resp="$(curl -s -w $'\n%{http_code}' -X POST "$url")"
  code="${resp##*$'\n'}"
  body="${resp%$'\n'*}"
  field="$(printf '%s' "$body" | python3 -c 'import sys,json
try: print(json.load(sys.stdin)["error"]["code"])
except Exception: print("-")' 2>/dev/null || echo -)"
  if [[ "$code" == "$want_code" ]] && { [[ -z "$want_code_field" ]] || [[ "$field" == "$want_code_field" ]]; }; then
    printf '  ok   %-22s %s %s\n' "$name" "$code" "$field"
  else
    printf '  FAIL %-22s got %s/%s want %s/%s\n' "$name" "$code" "$field" "$want_code" "$want_code_field"
    echo "       body: $body"
    fail=1
  fi
}

echo "== benign images (expect 200) =="
for img in delete-recreate opaque-shadow links-and-hardlinks plain-tar; do
  check "$img" 200 "" "$BASE/v1/images/$img/rebuild"
done

echo "== malicious images (expect 422 + code) =="
check link-escape     422 link_escape        "$BASE/v1/images/link-escape/rebuild"
check link-absolute   422 link_escape        "$BASE/v1/images/link-absolute/rebuild"
check path-traversal  422 path_traversal     "$BASE/v1/images/path-traversal/rebuild"
check absolute-path   422 path_traversal     "$BASE/v1/images/absolute-path/rebuild"
check device-file     422 unsafe_entry_type  "$BASE/v1/images/device-file/rebuild"
check corrupt-gzip    422 gzip_error         "$BASE/v1/images/corrupt-gzip/rebuild"
check garbage-gzip    422 tar_error          "$BASE/v1/images/garbage-gzip/rebuild"
check digest-tampered 422 digest_mismatch    "$BASE/v1/images/digest-tampered/rebuild"
check diffid-tampered 422 digest_mismatch    "$BASE/v1/images/diffid-tampered/rebuild"

echo "== no partial publish for malicious images =="
for img in link-escape link-absolute path-traversal absolute-path \
           device-file corrupt-gzip garbage-gzip digest-tampered diffid-tampered; do
  if [[ -e "$BD/$img/latest" ]]; then
    echo "  FAIL $img published a partial/complete root"; fail=1
  else
    echo "  ok   $img not published"
  fi
done

echo "== semantic spot checks on published benign roots =="
grep -qx "layer2-final" "$BD/delete-recreate/latest/rootfs/app/version.txt" \
  && echo "  ok   version.txt == layer2-final" || { echo "  FAIL version content"; fail=1; }
grep -qx "new after whiteout" "$BD/delete-recreate/latest/rootfs/app/will_be_deleted" \
  && echo "  ok   deleted-then-recreated content" || { echo "  FAIL recreate content"; fail=1; }
[[ ! -e "$BD/opaque-shadow/latest/rootfs/etc/app/old.cfg" ]] \
  && echo "  ok   opaque hid lower etc/app/old.cfg" || { echo "  FAIL opaque"; fail=1; }
[[ -f "$BD/opaque-shadow/latest/rootfs/etc/base.conf" ]] \
  && echo "  ok   opaque preserved sibling etc/base.conf" || { echo "  FAIL sibling"; fail=1; }

echo
if [[ "$fail" -eq 0 ]]; then
  echo "ACCEPTANCE PASSED"
else
  echo "ACCEPTANCE FAILED (server log: $LOG)"
  cat "$LOG" || true
  exit 1
fi
