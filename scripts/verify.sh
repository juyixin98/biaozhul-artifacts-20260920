#!/usr/bin/env bash
# Local verification for the restricted-Datalog service.
# Exit code 0 only when every check passes.
set -euo pipefail

cd "$(dirname "$0")/.."

DB_TMP="$(mktemp -d)/verify.db"
trap 'rm -f "$DB_TMP" "$DB_TMP-"* 2>/dev/null || true' EXIT

echo "== 1/5: compile all modules =="
python3 -m compileall -q app scripts

echo "== 2/5: unit/integration tests with coverage =="
python3 -m pytest --cov=app --cov-report=term-missing --cov-fail-under=80

echo "== 3/5: CLI compile (stratification diagnostics) =="
python3 -m scripts.cli --db "$DB_TMP" compile data/example.dl >/tmp/verify_compile.json
python3 - <<'PY'
import json
d = json.load(open("/tmp/verify_compile.json"))
assert d["status"] == "ok", d
levels = [p for s in d["strata"] for p in s["predicates"]]
assert "ancestor/2" in levels and "unreachable/1" in levels
print("   strata:", [(s["level"], s["predicates"], s["iterations"]) for s in d["strata"]])
PY

echo "== 4/5: CLI query + verifiable proof =="
python3 -m scripts.cli --db "$DB_TMP" query data/example.dl "ancestor(ann, X)" >/tmp/verify_query.json
python3 - <<'PY'
import json
d = json.load(open("/tmp/verify_query.json"))
assert d["status"] == "ok", d
xs = sorted(a["bindings"]["X"] for a in d["answers"])
assert xs == ["ben", "bob", "cy", "dan"], xs
assert d["failures"] == []
# every answer has a proof tree ending in EDB facts
def leaves_facts(n):
    c = n["children"]
    return (n["kind"] == "fact") if not c else all(leaves_facts(x) for x in c)
assert all(leaves_facts(a["proof"]) for a in d["answers"])
print("   answers:", xs, "| version:", d["version"])
PY

echo "== 5/5: compile-time rejections carry failure categories =="
python3 - <<'PY'
from app.service import DatalogService
from app.store.sqlite_store import EvidenceStore
svc = DatalogService(EvidenceStore(":memory:"))
checks = {
    "p(X) :- q(Y).": "unsafe_variable",
    "p(X) :- q(X), NOT r(Y).": "unsafe_variable",
    "p(X) :- q(X), NOT r(X).\nr(X) :- p(X).\nq(a).": "negation_cycle",
    "p(a).\np(a,b).": "arity_mismatch",
}
for src, cat in checks.items():
    r = svc.run_query(src, "p(X)", persist=False)
    assert r.error_category == cat, (src, r.error_category)
    print(f"   {cat:18} <- {src.splitlines()[0]}")
print("ALL CHECKS PASSED")
PY
