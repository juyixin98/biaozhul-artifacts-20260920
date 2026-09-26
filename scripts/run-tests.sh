#!/usr/bin/env bash
# Runs the full automated suite with -race and emits STRUCTURED results:
#   build/test.log     - raw go test -json stream
#   test-results.json  - machine-readable acceptance summary
#
# Usage: scripts/run-tests.sh [go test packages...]
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
OUTDIR="${OUTDIR:-$ROOT}"
LOG="$OUTDIR/build/test.log"
REPORT="$OUTDIR/test-results.json"
mkdir -p "$OUTDIR/build"

if [ "$#" -eq 0 ]; then
  PKGS=(./...)
else
  PKGS=("$@")
fi

echo ">> go vet"
go vet ./... || { echo "vet failed"; exit 1; }

echo ">> go test -race -json ${PKGS[*]}"
# Preserve the exit status of go test even though we pipe to tee.
set -o pipefail
go test -race -count=1 -json "${PKGS[@]}" 2>&1 | tee "$LOG" >/dev/null
STATUS=${PIPESTATUS[0]}

# Build a structured report from the JSON event stream using jq if present,
# otherwise fall back to a small awk parser.
if command -v jq >/dev/null 2>&1; then
  jq -s '
    {
      schema: "idemresp-test-results/v1",
      generated_at: (now | todate),
      overall: (if any(.[]; select(.Action=="fail")) then "FAIL" else "PASS" end),
      packages: (
        [ .[] | select(.Action=="pass" or .Action=="fail")
              | select(.Test == null) ]
        | map({ package: .Package, result: .Action, elapsed: .Elapsed })),
      tests: (
        [ .[] | select(.Test != null) | select(.Action=="pass" or .Action=="fail") ]
        | map({ test: .Test, package: .Package, result: .Action, elapsed: .Elapsed }))
    } ' "$LOG" > "$REPORT"
else
  awk '
    BEGIN { print "{"; print "  \"schema\": \"idemresp-test-results/v1\"," }
    { n++ }
    END { print "  \"note\": \"jq not installed; see build/test.log for the JSON stream\",", \
                 "  \"events\": " n+0; print "}" }
  ' "$LOG" > "$REPORT"
fi

PASS=$(grep -c '"Action":"pass"' "$LOG" || true)
FAIL=$(grep -c '"Action":"fail"' "$LOG" || true)
echo ">> pass events: $PASS  fail events: $FAIL"
echo ">> structured report: $REPORT"
echo ">> go test exit status: $STATUS"
exit "$STATUS"
