#!/usr/bin/env bash
# One-command validation: build, unit tests, CLI e2e, accuracy experiment.
# Tees everything to reports/validation.log so the run is recorded.
set -uo pipefail
cd "$(dirname "$0")/.."

mkdir -p reports
LOG=reports/validation.log
: > "$LOG"

section() { echo; echo "==================== $1 ===================="; }
run() {
  echo "\$ $*" | tee -a "$LOG"
  "$@" 2>&1 | tee -a "$LOG"
  return "${PIPESTATUS[0]}"
}

STATUS=0

section "1/4 build (plain JDK, zero dependencies)"
if run ./build.sh; then echo "[ok] build"; else echo "[FAIL] build"; STATUS=1; fi

section "2/4 built-in unit tests (63 cases)"
if run java -jar hllengine.jar selftest; then echo "[ok] unit tests"; else echo "[FAIL] unit tests"; STATUS=1; fi

section "3/4 CLI end-to-end tests"
if run bash tests/cli_e2e.sh; then echo "[ok] cli tests"; else echo "[FAIL] cli tests"; STATUS=1; fi

section "4/4 fixed-seed accuracy experiment (960 trials)"
if run java -jar hllengine.jar experiment --trials 40 --out-dir reports; then
  echo "[ok] experiment"; else echo "[FAIL] experiment"; STATUS=1; fi

section "DONE"
if [ "$STATUS" -eq 0 ]; then echo "ALL STAGES PASSED"; else echo "SOME STAGES FAILED (see above)"; fi
exit $STATUS
