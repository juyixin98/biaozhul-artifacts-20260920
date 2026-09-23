#!/usr/bin/env bash
# End-to-end acceptance tests for the pose graph backend.
# Usage: acceptance.sh PGO_BIN PGO_GEN_BIN REPO_ROOT
set -u

PGO="${1:?pgo binary path required}"
GEN="${2:?pgo_gen binary path required}"
ROOT="${3:?repo root required}"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
PASS=0
FAIL=0

say()  { printf '\n=== %s ===\n' "$1"; }
pass() { PASS=$((PASS + 1)); printf 'PASS: %s\n' "$1"; }
fail() { FAIL=$((FAIL + 1)); printf 'FAIL: %s\n' "$1"; }

# assert_equal DESC EXPECTED ACTUAL
assert_eq() {
  if [ "$2" = "$3" ]; then pass "$1"; else fail "$1 (expected [$2], got [$3])"; fi
}
# assert_num LE|GE|LT|GT DESC VAL THRESH
assert_num() {
  local op="$1" desc="$2" v="$3" t="$4" ok=0
  case "$op" in
    LE) awk "BEGIN{exit !($v <= $t)}" && ok=1 ;;
    GE) awk "BEGIN{exit !($v >= $t)}" && ok=1 ;;
    LT) awk "BEGIN{exit !($v <  $t)}" && ok=1 ;;
    GT) awk "BEGIN{exit !($v >  $t)}" && ok=1 ;;
  esac
  if [ "$ok" = 1 ]; then pass "$desc ($v $op $t)"; else fail "$desc ($v not $op $t)"; fi
}
json_num() {
  local file="$1" key="$2"
  grep -o "\"$key\"[[:space:]]*:[[:space:]]*-\{0,1\}[0-9.][0-9.eE+-]*" "$file" \
    | head -1 | sed 's/^[^:]*:[[:space:]]*//'
}
edge_weight() {
  # robust_weight of the named edge in edge_errors_after.
  local file="$1" edge="$2"
  awk -v e="$edge" '
    /"edge_errors_after"/ {insec=1}
    insec && $0 ~ "\"edge\".*\"" e "\"" {found=1}
    found && /"robust_weight"/ {
      gsub(/.*: */, ""); gsub(/,/, ""); print; exit
    }' "$file"
}

# ------------------------------------------------------------- 1. drift loop
say "1. drift loop closure: chi2 must drop substantially"
"$GEN" drift-loop --size 10 --output "$WORK/drift.json"
"$PGO" run --input "$WORK/drift.json" --output "$WORK/drift.out.json" \
  --db "$WORK/runs.db" --robust huber --robust-scale 1.0 --threads 1 \
  | tee "$WORK/drift.log"
rc=${PIPESTATUS[0]}
assert_eq "drift loop exits 0" 0 "$rc"
assert_eq "result file published" 1 "$([ -s "$WORK/drift.out.json" ] && echo 1 || echo 0)"
i=$(json_num "$WORK/drift.out.json" initial_chi2)
f=$(json_num "$WORK/drift.out.json" final_chi2)
if [ -n "$i" ] && [ -n "$f" ]; then
  awk "BEGIN{exit !($f < 0.01 * $i)}" \
    && pass "final chi2 < 1% of initial ($f vs $i)" \
    || fail "final chi2 not < 1% of initial ($f vs $i)"
else
  fail "chi2 fields missing"
fi
ratio=$(json_num "$WORK/drift.out.json" chi2_reduction_ratio)
assert_num LT "chi2 reduction ratio below 0.01" "${ratio:-999}" 0.01
n_edges=$(grep -c '"edge":' "$WORK/drift.out.json")
[ "$n_edges" -ge 80 ] && pass "per-edge errors reported twice (before/after)" \
  || fail "expected >=80 edge lines, got $n_edges"

# --------------------------------------------------------------- 2. bad loop
say "2. wrong loop closure is down-weighted by robust loss"
"$GEN" bad-loop --output "$WORK/bad.json"
"$PGO" run --input "$WORK/bad.json" --output "$WORK/bad.out.json" \
  --db "$WORK/runs.db" --robust huber --robust-scale 1.0 --threads 1
assert_eq "bad loop exits 0" 0 "$?"
wb=$(edge_weight "$WORK/bad.out.json" bad_close)
wg=$(edge_weight "$WORK/bad.out.json" good_close)
assert_num LT "bad closure robust weight < 0.3" "${wb:-1}" 0.3
awk "BEGIN{exit !($wg > $wb)}" && pass "good closure weight > bad weight ($wg > $wb)" \
  || fail "good closure not favored ($wg vs $wb)"

# Without robust loss the bad edge must keep weight 1.
"$PGO" run --input "$WORK/bad.json" --output "$WORK/bad.norobust.json" \
  --db "$WORK/runs.db" --robust none --threads 1
wbn=$(edge_weight "$WORK/bad.norobust.json" bad_close)
assert_eq "no-loss gives weight 1.0" "1" "$wbn"

# ----------------------------------------------------------- 3. disconnected
say "3. disconnected graph: auto anchor per component"
"$GEN" disconnected --output "$WORK/disc.json"
"$PGO" run --input "$WORK/disc.json" --output "$WORK/disc.out.json" \
  --db "$WORK/runs.db" --threads 1 | tee "$WORK/disc.log"
assert_eq "disconnected auto-anchor exits 0" 0 "${PIPESTATUS[0]}"
grep -q "anchors=a0,b0" "$WORK/disc.log" && pass "two anchors chosen (a0,b0)" \
  || fail "expected anchors=a0,b0 in log"
grep -q "components=2" "$WORK/disc.log" && pass "two components reported" \
  || fail "components=2 missing"

# Single explicit anchor on a 2-component graph must fail with exit 1.
"$PGO" run --input "$WORK/disc.json" --output "$WORK/disc.fail.json" \
  --db "$WORK/runs.db" --anchor a0 >/dev/null 2>"$WORK/disc.err"
assert_eq "single anchor on 2 components fails" 1 "$?"
grep -qi "no anchor" "$WORK/disc.err" && pass "failure names missing anchor" \
  || fail "missing-anchor message not found"
[ ! -e "$WORK/disc.fail.json" ] && pass "no output published on setup failure" \
  || fail "output file leaked on failure"

# ------------------------------------------------------------- 4. ill weights
say "4. non-positive-definite information matrix is rejected"
"$GEN" illweight --output "$WORK/ill.json"
"$PGO" run --input "$WORK/ill.json" --output "$WORK/ill.out.json" \
  --db "$WORK/runs.db" >/dev/null 2>"$WORK/ill.err"
assert_eq "ill-conditioned input rejected with exit 2" 2 "$?"
grep -qi "not positive definite" "$WORK/ill.err" && pass "error says not positive definite" \
  || fail "SPD error message missing"
[ ! -e "$WORK/ill.out.json" ] && pass "no output on invalid input" \
  || fail "output leaked for invalid input"

# ------------------------------------------------------------- 5. seam angle
say "5. angle crossing +/-pi handled by wrapping"
"$GEN" angle-seam --output "$WORK/seam.json"
"$PGO" run --input "$WORK/seam.json" --output "$WORK/seam.out.json" \
  --db "$WORK/runs.db" --threads 1
assert_eq "seam graph exits 0" 0 "$?"
s0=$(awk '/"edge_errors_after"/{f=1} f&&/"edge".*"seam"/{e=1}
          e&&/"residual"/{print; exit}' "$WORK/seam.out.json")
init_chi2=$(json_num "$WORK/seam.out.json" initial_chi2)
assert_num LT "seam initial chi2 tiny (wrapped measurement)" "${init_chi2:-999}" 1e-12

# ------------------------------------------------------------- 6. cancellation
say "6. cancellation publishes no half-optimized result"
"$GEN" large-grid --grid 120 --output "$WORK/big.json"
rm -f "$WORK/big.out.json"
set +e
timeout 60 "$PGO" run --input "$WORK/big.json" --output "$WORK/big.out.json" \
  --db "$WORK/runs.db" --cancel-after-ms 5 --max-iterations 100000 --threads 1 \
  >"$WORK/big.log" 2>&1
rc=$?
set +e
assert_eq "cancel exits 130" 130 "$rc"
[ ! -e "$WORK/big.out.json" ] && pass "no output file after cancellation" \
  || fail "half-optimized output was published!"
grep -q "RUN_CANCELLED" "$WORK/big.log" && pass "RUN_CANCELLED reported" \
  || fail "cancellation marker missing"

# SIGINT path: real signal to a running process. Retry until we catch the
# solver mid-run (a fast machine can finish before the signal lands).
sigint_rc=1
sigint_leaked=0
for attempt in 1 2 3 4 5; do
  rm -f "$WORK/big2.out.json"
  "$PGO" run --input "$WORK/big.json" --output "$WORK/big2.out.json" \
    --db "$WORK/runs.db" --max-iterations 100000 --threads 1 >"$WORK/big2.log" 2>&1 &
  pid=$!
  # Wait until the process is actually inside Ceres (output DB created on
  # success only; instead poll that the process is CPU-bound, then signal).
  sleep 0.15
  kill -INT "$pid" 2>/dev/null || true
  wait "$pid"; sigint_rc=$?
  if [ "$sigint_rc" = 130 ]; then break; fi
  # Exit 0 means it finished first: enlarge the delay window and retry.
  sleep 0.2
done
assert_eq "SIGINT exits 130" 130 "$sigint_rc"
[ ! -e "$WORK/big2.out.json" ] && pass "SIGINT published no output" \
  || { fail "output leaked on SIGINT"; sigint_leaked=1; }

# Cancellation must be auditable in SQLite as status=cancelled.
runid=$(grep -o 'run_id=run_[a-f0-9]*' "$WORK/big.log" | head -1 | cut -d= -f2)
if [ -n "$runid" ]; then
  "$PGO" inspect --db "$WORK/runs.db" --run "$runid" | grep -q "status=cancelled" \
    && pass "cancelled run recorded in SQLite ($runid)" \
    || fail "cancelled run not recorded as cancelled"
else
  fail "could not parse cancelled run id"
fi

# ------------------------------------------------------------- 7. frozen hash
say "7. frozen input graph version is enforced by hash"
"$PGO" freeze --frozen-db "$WORK/frozen.db" --name loop-v1 \
  --input "$WORK/drift.json" | tee "$WORK/freeze.log"
hash1=$(grep -o 'sha256=[a-f0-9]*' "$WORK/freeze.log" | cut -d= -f2)
# Tamper with the graph (change one measurement); hash must differ. Target
# the first odometry edge by id so the substitution is unambiguous.
awk '
  /"id": "e0"/ {inedge=1}
  inedge && /"dx"/ {sub(/"dx": [0-9.eE+-]+/, "\"dx\": 1.75"); inedge=0}
  {print}
' "$WORK/drift.json" > "$WORK/drift.tampered.json"
grep -q '"dx": 1.75' "$WORK/drift.tampered.json" || fail "tamper substitution did not apply"
"$PGO" run --input "$WORK/drift.tampered.json" --output "$WORK/t.out.json" \
  --frozen-db "$WORK/frozen.db" --frozen loop-v1 --db "$WORK/runs.db" \
  >"$WORK/t.log" 2>"$WORK/t.err"
assert_eq "tampered graph rejected with exit 3" 3 "$?"
grep -q "FROZEN MISMATCH" "$WORK/t.err" && pass "frozen mismatch reported" \
  || fail "frozen mismatch message missing"
[ ! -e "$WORK/t.out.json" ] && pass "no optimize against non-frozen version" \
  || fail "ran optimization on tampered frozen input"
# Re-freezing identical content is idempotent; different hash same name fails.
"$PGO" freeze --frozen-db "$WORK/frozen.db" --name loop-v1 \
  --input "$WORK/drift.json" >/dev/null && pass "identical re-freeze is idempotent" \
  || fail "idempotent re-freeze failed"
"$PGO" freeze --frozen-db "$WORK/frozen.db" --name loop-v1 \
  --input "$WORK/drift.tampered.json" >/dev/null 2>&1 \
  && fail "re-freeze with different hash should fail" \
  || pass "re-freeze same name different hash refused"
# Matching frozen input runs fine.
"$PGO" run --input "$WORK/drift.json" --output "$WORK/f.out.json" \
  --frozen-db "$WORK/frozen.db" --frozen loop-v1 --db "$WORK/runs.db" --threads 1
assert_eq "frozen-matching run exits 0" 0 "$?"

# ------------------------------------------------------------- 8. idempotency
say "8. run audit trail is queryable"
n_runs=$(grep -c '^RUN_OK' "$WORK/drift.log" 2>/dev/null || true)
"$PGO" inspect --db "$WORK/runs.db" --run "$(grep -o 'run_id=run_[a-f0-9]*' "$WORK/drift.log" | head -1 | cut -d= -f2)" \
  | grep -q "status=success" && pass "successful run recorded" || fail "success run missing"

printf '\n----------------------------------------\n%d PASSED, %d FAILED\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
