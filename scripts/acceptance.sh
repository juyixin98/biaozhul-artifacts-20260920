#!/usr/bin/env bash
# End-to-end acceptance suite for the pgo SE(2) backend.
# Covers: drift+loop closure, false loop (robust loss), disconnected graph,
# ill-conditioned weights, non-SPD rejection, angle wrap, tamper/HMAC,
# cancellation (deadline + SIGTERM) with no half-optimized result.
set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BIN="$ROOT/build/pgo"
WORK="$(mktemp -d)"
DB="$WORK/runs.db"
PASS=0
FAIL=0

trap 'rm -rf "$WORK"' EXIT

ok()   { printf '  [PASS] %s\n' "$1"; PASS=$((PASS+1)); }
fail() { printf '  [FAIL] %s\n' "$1"; FAIL=$((FAIL+1)); }

if [ ! -x "$BIN" ]; then
  echo "binary $BIN missing — build first: cmake --build build"
  exit 1
fi

echo "== 1. drift + loop closure: residual must drop and loop closes =="
"$BIN" run --input "$ROOT/examples/drift_loop.json" --result "$WORK/drift.json" \
    --db "$DB" > "$WORK/drift.out" 2>"$WORK/drift.err"
rc=$?
if [ $rc -eq 0 ]; then ok "run exits 0"; else fail "run exits 0 (rc=$rc)"; cat "$WORK/drift.err"; fi
python3 - "$WORK/drift.json" <<'PY'
import json, sys, math
d = json.load(open(sys.argv[1]))
res = d["body"]["residual"]
i, f = res["initial"]["weighted_cost"], res["final"]["weighted_cost"]
assert f < i * 1e-4, (i, f)
assert f < 1e-6, f
assert d["body"]["status"] == "solved"
assert d["body"]["summary"]["num_components"] == 1
n_edges = len(d["body"]["edges"]["final"])
assert n_edges == 14, n_edges
n0 = [n for n in d["body"]["nodes"] if n["id"] == "n0"][0]
assert n0["anchor"] and n0["init"] == n0["final"]
# the closing node returns to the anchor's position (loop closes)
last = [n for n in d["body"]["nodes"] if n["id"] == "n13"][0]["final"]
assert math.hypot(last[0], last[1]) < 0.01, last
# corners land on the exact 3x2 rectangle
n3 = [n for n in d["body"]["nodes"] if n["id"] == "n3"][0]["final"]
n7 = [n for n in d["body"]["nodes"] if n["id"] == "n7"][0]["final"]
assert math.hypot(n3[0]-3, n3[1]) < 0.01 and abs(n3[2]) < 0.01, n3
assert math.hypot(n7[0]-3, n7[1]-2) < 0.01, n7
print("drift checks ok: %.6g -> %.6g (%d edges)" % (i, f, n_edges))
PY
[ $? -eq 0 ] && ok "drift residual/closure assertions" || fail "drift residual/closure assertions"

echo "== 2. false loop closure: robust loss protects good edges =="
"$BIN" run --input "$ROOT/examples/bad_loop.json" --result "$WORK/bad.json" \
    --db "$DB" > "$WORK/bad.out" 2>"$WORK/bad.err"
rc=$?
[ $rc -eq 0 ] && ok "bad_loop run exits 0" || fail "bad_loop run failed rc=$rc"
python3 - "$WORK/bad.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
edges = {e["edge_id"]: e for e in d["body"]["edges"]["final"]}
good = ["ap", "pe", "ab", "bc", "cd", "da"]
good_sum = sum(edges[e]["weighted_squared"] for e in good)
bad = edges["false-loop-e-d"]
print("good_sum=%.5f bad_ws=%.3f bad_robust=%.4f"
      % (good_sum, bad["weighted_squared"], bad["robust_cost"]))
assert good_sum < 1.0, good_sum
assert bad["weighted_squared"] > 100.0, bad["weighted_squared"]
# robust cost of the outlier is heavily discounted vs quadratic
assert bad["robust_cost"] < 0.05 * bad["weighted_squared"], bad
PY
[ $? -eq 0 ] && ok "false loop discounted by Huber" || fail "false loop discounted by Huber"

# without robust loss: good edges must be substantially more distorted
python3 - "$ROOT/examples/bad_loop.json" "$WORK/bad_loop_quad.json" <<'PY'
import json, sys
src, dst = sys.argv[1], sys.argv[2]
d = json.load(open(src))
d["options"]["loss_type"] = "none"
for e in d["edges"]:
    e.pop("loss_type", None); e.pop("loss_param", None)
json.dump(d, open(dst, "w"))
PY
"$BIN" run --input "$WORK/bad_loop_quad.json" --result "$WORK/badq.json" \
    --db "$DB" >/dev/null 2>&1
python3 - "$WORK/bad.json" "$WORK/badq.json" <<'PY'
import json, sys
rob = json.load(open(sys.argv[1]))
qua = json.load(open(sys.argv[2]))
good = {"ap", "pe", "ab", "bc", "cd", "da"}
def good_sum(d):
    return sum(e["weighted_squared"] for e in d["body"]["edges"]["final"]
               if e["edge_id"] in good)
gr, gq = good_sum(rob), good_sum(qua)
print("good distortion: robust=%.5g quadratic=%.5g" % (gr, gq))
assert gr < 0.1 * gq, (gr, gq)
assert gq > 10.0, gq
PY
[ $? -eq 0 ] && ok "robust beats quadratic on good edges" || fail "robust beats quadratic on good edges"

echo "== 3. disconnected graph: per-component anchoring works; single fails =="
"$BIN" run --input "$ROOT/examples/disconnected.json" --result "$WORK/disc.json" \
    --db "$DB" >/dev/null 2>"$WORK/disc.err"
rc=$?
[ $rc -eq 0 ] && ok "per-component run succeeds" || { fail "per-component run (rc=$rc)"; cat "$WORK/disc.err"; }
python3 - "$WORK/disc.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
assert d["body"]["summary"]["num_components"] == 2
assert len(d["body"]["summary"]["anchors"]) == 2
assert d["body"]["residual"]["final"]["weighted_cost"] < 1e-6
anchors = sorted(d["body"]["summary"]["anchors"])
assert anchors == ["a0", "b0"], anchors
PY
[ $? -eq 0 ] && ok "two components each anchored" || fail "two components each anchored"

python3 - "$ROOT/examples/disconnected.json" "$WORK/disc_single.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
d["options"]["anchor_mode"] = "single"
json.dump(d, open(sys.argv[2], "w"))
PY
"$BIN" run --input "$WORK/disc_single.json" --result "$WORK/should_not_exist.json" \
    --db "$DB" >/dev/null 2>"$WORK/disc_single.err"
rc=$?
[ $rc -eq 3 ] && ok "single-anchor disconnected exits 3" || fail "expected exit 3, got $rc"
[ ! -e "$WORK/should_not_exist.json" ] && ok "no result on disconnected failure" \
    || fail "result file must not exist"
grep -q "graph.disconnected" "$WORK/disc_single.err" && ok "error code reported" \
    || fail "expected graph.disconnected error"

echo "== 4. ill-conditioned weights: warning + solve completes =="
"$BIN" run --input "$ROOT/examples/ill_conditioned.json" --result "$WORK/ill.json" \
    --db "$DB" >/dev/null 2>"$WORK/ill.err"
rc=$?
[ $rc -eq 0 ] && ok "ill-conditioned run exits 0" || fail "ill-conditioned run rc=$rc"
grep -q "ill_conditioned" "$WORK/ill.err" && ok "ill-conditioned warning emitted" \
    || fail "missing ill-conditioned warning"

echo "== 5. non-SPD information: rejected, exit 2, no result, DB records it =="
"$BIN" run --input "$ROOT/examples/not_spd.json" --result "$WORK/nospd.json" \
    --db "$DB" >/dev/null 2>"$WORK/nospd.err"
rc=$?
[ $rc -eq 2 ] && ok "non-SPD exits 2" || fail "non-SPD expected exit 2, got $rc"
[ ! -e "$WORK/nospd.json" ] && ok "no result for non-SPD" || fail "result leaked for non-SPD"
grep -q "not_positive_definite" "$WORK/nospd.err" && ok "SPD rejection reason shown" \
    || fail "SPD reason missing"

echo "== 6. angles crossing +/- pi =="
"$BIN" run --input "$ROOT/examples/angle_wrap.json" --result "$WORK/wrap.json" \
    --db "$DB" >/dev/null 2>&1
rc=$?
[ $rc -eq 0 ] && ok "angle-wrap run exits 0" || fail "angle-wrap run rc=$rc"
python3 - "$WORK/wrap.json" <<'PY'
import json, sys, math
d = json.load(open(sys.argv[1]))
assert d["body"]["residual"]["final"]["weighted_cost"] < 1e-6
for e in d["body"]["edges"]["final"]:
    assert -math.pi <= e["error"][2] <= math.pi, e
for n in d["body"]["nodes"]:
    assert -math.pi <= n["final"][2] <= math.pi, n
# the raw heading crossing +/- pi was handled (no ~2pi residual)
PY
[ $? -eq 0 ] && ok "wrap residuals converge within (-pi,pi]" || fail "wrap check failed"

echo "== 7. hashing, tamper evidence, HMAC =="
"$BIN" run --input "$ROOT/examples/drift_loop.json" --result "$WORK/signed.json" \
    --db "$DB" --signing-key "acceptance-secret" >/dev/null 2>&1
"$BIN" verify --result "$WORK/signed.json" --signing-key "acceptance-secret" >/dev/null 2>&1
[ $? -eq 0 ] && ok "HMAC verify with correct key" || fail "HMAC verify failed"
"$BIN" verify --result "$WORK/signed.json" --signing-key "wrong" >/dev/null 2>&1
[ $? -eq 6 ] && ok "HMAC verify rejects wrong key" || fail "wrong key must fail"
python3 - "$WORK/signed.json" <<'PY'
import json, sys
p = sys.argv[1]
d = json.load(open(p))
d["body"]["nodes"][1]["final"][0] += 0.5   # tamper after signing
json.dump(d, open(p, "w"))
PY
"$BIN" verify --result "$WORK/signed.json" --signing-key "acceptance-secret" >/dev/null 2>&1
[ $? -eq 6 ] && ok "tampered body detected" || fail "tamper not detected"
"$BIN" run --input "$ROOT/examples/drift_loop.json" --result "$WORK/unsigned.json" \
    --db "$DB" >/dev/null 2>&1
"$BIN" verify --result "$WORK/unsigned.json" >/dev/null 2>&1
[ $? -eq 0 ] && ok "unsigned report verifies via SHA-256" || fail "unsigned verify failed"
h1=$("$BIN" run --input "$ROOT/examples/drift_loop.json" --result "$WORK/a1.json" --db "$DB" 2>/dev/null | grep input_sha256 | cut -d= -f2)
h2=$("$BIN" run --input "$ROOT/examples/drift_loop.json" --result "$WORK/a2.json" --db "$DB" 2>/dev/null | grep input_sha256 | cut -d= -f2)
if [ "$h1" = "$h2" ] && [ -n "$h1" ]; then ok "frozen input hash deterministic"; else fail "input hash mismatch"; fi
echo "     input_sha256=$h1"

echo "== 8. cancellation: deadline and SIGTERM never publish half results =="
"$BIN" generate --out "$WORK/big.json" --nodes 2000 --loops 40 --noise 0.02 \
    --seed 7 --graph-version stress-1 >/dev/null
# 8a: deadline cancellation
"$BIN" run --input "$WORK/big.json" --result "$WORK/big_out.json" --db "$DB" \
    --cancel-after-ms 5 >"$WORK/cancel.out" 2>"$WORK/cancel.err"
rc=$?
[ $rc -eq 4 ] && ok "deadline cancellation exits 4" || fail "expected 4, got $rc"
[ ! -e "$WORK/big_out.json" ] && ok "no result file after deadline cancel" \
    || fail "half-optimized result leaked"
grep -q "status=cancelled" "$WORK/cancel.out" && ok "cancelled run announced" \
    || fail "missing cancelled status"
"$BIN" history --db "$DB" --limit 50 | grep -q cancelled \
    && ok "cancellation recorded in SQLite" || fail "cancellation missing from DB"

# 8b: SIGTERM mid-solve (retry up to 3 times for timing robustness)
sig_ok=no
for attempt in 1 2 3; do
  "$BIN" run --input "$WORK/big.json" --result "$WORK/sig_out.json" --db "$DB" \
      --sleep-before-ms 300 >"$WORK/sig.out" 2>"$WORK/sig.err" &
  pid=$!
  sleep 0.15
  kill -TERM "$pid" 2>/dev/null
  wait "$pid"
  rc=$?
  if [ $rc -eq 4 ] && [ ! -e "$WORK/sig_out.json" ]; then
    sig_ok=yes
    break
  fi
done
[ "$sig_ok" = "yes" ] && ok "SIGTERM cancels with exit 4 and no result" \
    || fail "SIGTERM cancellation did not behave (rc=$rc)"

echo "== 9. initial/final per-edge errors are reported for every edge =="
python3 - "$ROOT/examples/drift_loop.json" "$WORK/drift.json" <<'PY'
import json, sys
src = json.load(open(sys.argv[1]))
d = json.load(open(sys.argv[2]))
src_ids = [e["id"] for e in src["edges"]]
for phase in ("initial", "final"):
    got = [e["edge_id"] for e in d["body"]["edges"][phase]]
    assert got == src_ids, (phase, got, src_ids)
    for e in d["body"]["edges"][phase]:
        assert len(e["error"]) == 3
        assert e["raw_norm"] >= 0
ri = sum(e["weighted_squared"] for e in d["body"]["edges"]["initial"])
rf = sum(e["weighted_squared"] for e in d["body"]["edges"]["final"])
assert rf < ri * 1e-4
print("per-edge report ok for %d edges (sum %.6g -> %.6g)" % (len(src_ids), ri, rf))
PY
[ $? -eq 0 ] && ok "per-edge initial/final errors complete" || fail "per-edge report failed"

echo
echo "========================================================"
echo " acceptance: $PASS passed, $FAIL failed"
echo "========================================================"
[ $FAIL -eq 0 ]
