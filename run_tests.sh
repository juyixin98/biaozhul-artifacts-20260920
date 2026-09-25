#!/usr/bin/env bash
# Run every check for the LattLang constant-propagation toolchain and print
# an honest per-category report. Exits non-zero if anything fails.
set -u
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
echo "=== interpreter ==="
"$PY" --version

echo
echo "=== 1/4 unit/integration tests ==="
"$PY" -m unittest discover -s tests -p 'test_*.py' -v 2>&1 | tail -n 12
unit_status=${PIPESTATUS[0]}

echo
echo "=== 2/4 acceptance examples: three-way equivalence (AST / IR / optimized) ==="
fail=0
for f in examples/*.lat; do
  out=$("$PY" -m lattlang.cli check "$f" 2>/dev/null)
  eq=$(echo "$out" | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["equivalent"])' 2>/dev/null)
  if [ "$eq" = "True" ]; then
    printf '  [PASS] %s\n' "$(basename "$f")"
  else
    printf '  [FAIL] %s\n' "$(basename "$f")"
    fail=1
  fi
done

echo
echo "=== 3/4 safety spot checks (div-by-zero behavior must be preserved) ==="
check_span () { # file -> expects equivalent div-by-zero trap with a span
  local f=$1
  "$PY" -m lattlang.cli check "$f" 2>/dev/null | "$PY" -c '
import json,sys
f=sys.argv[1]; d=json.load(sys.stdin)
ast=d["signatures"]["ast"]; span=ast["error_span"]
good=(d["equivalent"] and ast["error_message"]=="division or modulo by zero"
      and span is not None)
loc=""
if span:
    s=span["start"]; loc="  (trap at line %s col %s)" % (s["line"], s["col"])
print(("  [PASS] " if good else "  [FAIL] ")+f+loc)
sys.exit(0 if good else 1)' "$f"
  [ $? -ne 0 ] && fail=1
}
check_span examples/divzero_reachable.lat
check_span examples/divzero_dynamic.lat
# unreachable div-by-zero must NOT trap and must print the survivor value
"$PY" -m lattlang.cli check examples/divzero_unreachable.lat 2>/dev/null | "$PY" -c '
import json,sys
d=json.load(sys.stdin)
opt=d["signatures"]["optimized"]
good=(d["equivalent"] and opt["output"]==["5"]
      and opt["error_message"] is None)
print("  ["+("PASS" if good else "FAIL")+"] divzero_unreachable.lat  (dead /0 removed, prints 5)")
sys.exit(0 if good else 1)'
[ $? -ne 0 ] && fail=1

echo
echo "=== 4/4 JSON service smoke tests ==="
for f in examples/requests/*.json; do
  resp=$("$PY" -m lattlang.cli serve "$f" 2>/dev/null)
  ok=$(echo "$resp" | "$PY" -c 'import json,sys; print(json.load(sys.stdin).get("ok"))' 2>/dev/null)
  # error_parse is *expected* to return ok:false
  base=$(basename "$f")
  if [ "$base" = "error_parse.json" ]; then
    [ "$ok" = "False" ] && printf '  [PASS] %s (expected ok=false)\n' "$base" \
                       || { printf '  [FAIL] %s\n' "$base"; fail=1; }
  else
    [ "$ok" = "True" ] && printf '  [PASS] %s\n' "$base" \
                     || { printf '  [FAIL] %s\n' "$base"; fail=1; }
  fi
done

echo
if [ "$unit_status" -eq 0 ] && [ "$fail" -eq 0 ]; then
  echo "ALL CHECKS PASSED"
  exit 0
fi
echo "SOME CHECKS FAILED (unit_status=$unit_status, fail=$fail)"
exit 1
