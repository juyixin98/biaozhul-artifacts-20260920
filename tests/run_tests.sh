#!/usr/bin/env bash
# Automated tests for diffc. Requires python3 (used only for JSON assertions).
set -u
cd "$(dirname "$0")/.."

BIN=./diffc
PASS=0
FAIL=0

ok()   { PASS=$((PASS+1)); echo "PASS: $1"; }
bad()  { FAIL=$((FAIL+1)); echo "FAIL: $1"; }

# check <name> <expected-exit-code> <python-assertion-file> ; input on stdin
check() {
  local name="$1" want_rc="$2" assert="$3"
  local out rc
  out=$(cat | $BIN ${MODE} 2>/dev/null); rc=$?
  if [ "$rc" -ne "$want_rc" ]; then
    bad "$name (exit code $rc, want $want_rc)"
    echo "  output: $out" | head -5
    return
  fi
  if [ -n "$assert" ]; then
    if ! echo "$out" | python3 "$assert"; then
      bad "$name (assertion failed)"
      echo "  output: $out" | head -20
      return
    fi
  fi
  ok "$name"
}

mkdir -p tests/tmp

# ---------- assertion snippets ----------
cat > tests/tmp/assert_feasible.py <<'EOF'
import json,sys
r=json.load(sys.stdin)
assert r["status"]=="feasible", r
assert r["verification"]["satisfied"] is True
assert r["verification"]["violated"]==[]
a=r["assignment"]
assert min(a.values())==0, "not normalized"
sys.exit(0)
EOF

cat > tests/tmp/assert_zero_cycle_tight.py <<'EOF'
import json,sys
r=json.load(sys.stdin)
assert r["status"]=="feasible"
a=r["assignment"]
# zero cycle forces exact differences: a-b==5, b-c==2, c-a==-7
assert a["a"]-a["b"]==5 and a["b"]-a["c"]==2 and a["c"]-a["a"]==-7, a
sys.exit(0)
EOF

cat > tests/tmp/assert_infeasible.py <<'EOF'
import json,sys
r=json.load(sys.stdin)
assert r["status"]=="infeasible", r
cyc=r["negative_cycle"]
assert cyc["total_weight"]<0
assert set(cyc["constraint_ids"])=={"k1","k2","k3"}, cyc
assert set(r["minimal_infeasible_subset"])=={"k1","k2","k3"}
sys.exit(0)
EOF

cat > tests/tmp/assert_disconnected.py <<'EOF'
import json,sys
r=json.load(sys.stdin)
assert r["status"]=="feasible"
a=r["assignment"]
# two disconnected components: {a,b} and {c}; every var must be assigned
assert set(a)=={"a","b","c"}
assert a["a"]-a["b"]<=4
assert min(a.values())==0
sys.exit(0)
EOF

cat > tests/tmp/assert_verify_ok.py <<'EOF'
import json,sys
r=json.load(sys.stdin)
assert r["satisfied"] is True and r["violated"]==[]
sys.exit(0)
EOF

cat > tests/tmp/assert_verify_bad.py <<'EOF'
import json,sys
r=json.load(sys.stdin)
assert r["satisfied"] is False
assert set(r["violated"])=={"c1","c4"}, r
sys.exit(0)
EOF

cat > tests/tmp/assert_diagnose_two.py <<'EOF'
import json,sys
r=json.load(sys.stdin)
assert r["status"]=="infeasible"
got=sorted(tuple(sorted(c["constraint_ids"])) for c in r["minimal_infeasible_candidates"])
assert got==[("m1","m2"),("m3","m4")], got
assert r["enumeration"]["performed"] is True
assert r["enumeration"]["complete"] is True
sys.exit(0)
EOF

cat > tests/tmp/assert_diagnose_feasible.py <<'EOF'
import json,sys
r=json.load(sys.stdin)
assert r["status"]=="feasible"
assert r["minimal_infeasible_candidates"]==[]
sys.exit(0)
EOF

cat > tests/tmp/assert_error.py <<'EOF'
import json,sys
r=json.load(sys.stdin)
assert "error" in r, r
sys.exit(0)
EOF

# ---------- 1. feasible chain with zero cycle ----------
MODE=solve
check "solve: feasible with zero cycle (tight)" 0 tests/tmp/assert_zero_cycle_tight.py < samples/solve_feasible.json

# ---------- 2. infeasible: negative cycle with constraint ids ----------
check "solve: infeasible, negative cycle ids" 0 tests/tmp/assert_infeasible.py < samples/solve_infeasible.json

# ---------- 3. disconnected variables ----------
cat > tests/tmp/disc.json <<'EOF'
{"variables":["a","b","c"],
 "constraints":[{"id":"d1","var":"a","minus":"b","bound":4}]}
EOF
check "solve: disconnected variables" 0 tests/tmp/assert_disconnected.py < tests/tmp/disc.json

# ---------- 4. single variable, no constraints ----------
cat > tests/tmp/single.json <<'EOF'
{"variables":["solo"],"constraints":[]}
EOF
check "solve: single variable, no constraints" 0 tests/tmp/assert_feasible.py < tests/tmp/single.json

# ---------- 5. self-contradiction x - x <= -1 ----------
cat > tests/tmp/selfloop.json <<'EOF'
{"variables":["x"],
 "constraints":[{"id":"s1","var":"x","minus":"x","bound":-1}]}
EOF
cat > tests/tmp/assert_selfloop.py <<'EOF'
import json,sys
r=json.load(sys.stdin)
assert r["status"]=="infeasible"
assert r["negative_cycle"]["constraint_ids"]==["s1"]
assert r["negative_cycle"]["total_weight"]==-1
sys.exit(0)
EOF
check "solve: self-contradiction x-x<=-1" 0 tests/tmp/assert_selfloop.py < tests/tmp/selfloop.json

# ---------- 6. verify mode ----------
MODE=verify
check "verify: satisfying assignment" 0 tests/tmp/assert_verify_ok.py < samples/verify_ok.json
check "verify: violated constraint ids" 0 tests/tmp/assert_verify_bad.py < samples/verify_bad.json

# ---------- 7. diagnose mode ----------
MODE=diagnose
check "diagnose: two independent minimal conflicts" 0 tests/tmp/assert_diagnose_two.py < samples/diagnose_two_conflicts.json
check "diagnose: feasible system has no candidates" 0 tests/tmp/assert_diagnose_feasible.py < samples/solve_feasible.json

# ---------- 8. error handling ----------
MODE=solve
echo '{not json' | check "error: malformed JSON" 1 tests/tmp/assert_error.py
cat > tests/tmp/unknown_var.json <<'EOF'
{"variables":["a"],"constraints":[{"var":"a","minus":"ghost","bound":1}]}
EOF
check "error: unknown variable" 1 tests/tmp/assert_error.py < tests/tmp/unknown_var.json
cat > tests/tmp/dup_id.json <<'EOF'
{"variables":["a","b"],
 "constraints":[{"id":"q","var":"a","minus":"b","bound":1},
                {"id":"q","var":"b","minus":"a","bound":1}]}
EOF
check "error: duplicate constraint id" 1 tests/tmp/assert_error.py < tests/tmp/dup_id.json

# ---------- 9. scale limit ----------
python3 - <<'EOF' > tests/tmp/too_many.json
import json
print(json.dumps({"variables":[f"v{i}" for i in range(5001)],"constraints":[]}))
EOF
check "error: exceeds variable limit" 1 tests/tmp/assert_error.py < tests/tmp/too_many.json

# ---------- 10. max-scale stress (5000 vars, 20000 constraints) ----------
python3 - <<'EOF' > tests/tmp/stress.json
import json
n=5000
cons=[{"id":f"e{i}","var":f"v{i+1}","minus":f"v{i}","bound":3} for i in range(n-1)]
cons+=[{"id":f"b{i}","var":f"v{i%n}","minus":f"v{(i*7+13)%n}","bound":100} for i in range(20000-(n-1))]
print(json.dumps({"variables":[f"v{i}" for i in range(n)],"constraints":cons}))
EOF
check "solve: max-scale stress (5000 vars / 20000 constraints)" 0 tests/tmp/assert_feasible.py < tests/tmp/stress.json

# ---------- 11. randomized selftest: solver vs naive reference ----------
out=$($BIN selftest 3000 12345); rc=$?
if [ "$rc" -eq 0 ] && echo "$out" | python3 -c '
import json,sys
r=json.load(sys.stdin)
assert r["failures"]==0
assert r["feasible"]>0 and r["infeasible"]>0
assert r["reference_brute_forced"]>0
print("  selftest:",r)
'; then
  ok "selftest: 3000 randomized rounds vs naive reference"
else
  bad "selftest (rc=$rc): $out"
fi

echo
echo "passed=$PASS failed=$FAIL"
[ "$FAIL" -eq 0 ]
