#!/usr/bin/env bash
# End-to-end acceptance: boot the API, audit every example, assert verdicts.
# Exits non-zero on the first failed expectation.
set -uo pipefail

PORT="${PORT:-8923}"
BASE="http://127.0.0.1:${PORT}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"

pass=0; fail=0
check() { # desc, expected_substring, json_file
  local desc="$1" want="$2" file="$3"
  if grep -q "$want" "$file"; then
    printf '  ok   %s\n' "$desc"; pass=$((pass+1))
  else
    printf '  FAIL %s (wanted %q)\n' "$desc" "$want"; cat "$file"; fail=$((fail+1))
  fi
}

echo "Starting uvicorn on ${BASE} ..."
python -m uvicorn app.lockgate.web:app --host 127.0.0.1 --port "$PORT" >/tmp/lg-accept.log 2>&1 &
SRV=$!
cleanup() { kill "$SRV" 2>/dev/null || true; }
trap cleanup EXIT

# wait for readiness
for _ in $(seq 1 30); do
  curl -sf "${BASE}/health" >/dev/null 2>&1 && break
  sleep 0.3
done
curl -sf "${BASE}/health" >/dev/null || { echo "server failed to start"; cat /tmp/lg-accept.log; exit 1; }

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"; cleanup' EXIT

echo "== health =="
curl -s "${BASE}/health" -o "$tmp/h"; check "health ok" '"status":"ok"' "$tmp/h"

post() { curl -s -F "bundle=@$1" ${2:+-F os=$2 -F cpu=$3} "${BASE}/api/v1/audit"; }

echo "== scenario verdicts =="
post examples/01-clean-lock/bundle.tar.gz                       > "$tmp/01"; check "01 clean reproducible"   '"reproducible":true' "$tmp/01"
post examples/02-lock-drift/bundle.tar.gz                       > "$tmp/02"; check "02 range drift"          'range-drift'          "$tmp/02"
post examples/03-duplicate-versions/bundle.tar.gz               > "$tmp/03"; check "03 duplicate versions"   'duplicate-versions'   "$tmp/03"
post examples/04-missing-integrity/bundle.tar.gz                > "$tmp/04"; check "04 missing integrity"    'missing-or-bad-integrity' "$tmp/04"
post examples/05-platform-optional/bundle.tar.gz linux x64      > "$tmp/05"; check "05 platform excluded"    'platform-excluded'    "$tmp/05"
post examples/06-cycle/bundle.tar.gz                            > "$tmp/06"; check "06 cycle detected"       'dependency-cycle'     "$tmp/06"
post examples/07-unsupported-syntax/bundle.tar.gz               > "$tmp/07"; check "07 unsupported syntax"   'unsupported-syntax'   "$tmp/07"
post examples/08-workspaces/bundle.tar.gz                       > "$tmp/08"; check "08 workspace link"       '"workspace_links":2' "$tmp/08"
post examples/09-tampered-artifact/bundle.tar.gz                > "$tmp/09"; check "09 artifact tampered"    'artifact-tampered'    "$tmp/09"
post examples/10-lock-only-no-artifacts/bundle.tar.gz           > "$tmp/10"
check "10 lock present but NOT reproducible" '"content_verified":false' "$tmp/10"
grep -q '"reproducible":false' "$tmp/10" && { printf '  ok   10 reproducible=false despite lock\n'; pass=$((pass+1)); } || { printf '  FAIL 10\n'; fail=$((fail+1)); }

echo "== safety =="
printf 'not an archive' > "$tmp/junk"
code=$(curl -s -o "$tmp/safe" -w '%{http_code}' -F "bundle=@$tmp/junk;filename=x.tar.gz" "${BASE}/api/v1/audit")
[ "$code" = "422" ] && { printf '  ok   garbage archive -> 422\n'; pass=$((pass+1)); } || { printf '  FAIL garbage -> %s\n' "$code"; fail=$((fail+1)); }

echo
echo "passed=$pass failed=$fail"
[ "$fail" -eq 0 ]
