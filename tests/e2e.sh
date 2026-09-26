#!/usr/bin/env bash
# End-to-end tests for the treewidth JSON backend.
# Drives the compiled binary over stdin/files and asserts on the JSON
# responses with python3. Exits non-zero on the first failed test group.
set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BIN="$ROOT/build/treewidth"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

if [[ ! -x "$BIN" ]]; then
    echo "binary not found at $BIN; run 'make all' first" >&2
    exit 2
fi

PASS=0
FAIL=0

run() { # run NAME REQUEST_JSON  -> writes response to $TMP/$NAME.out
    local name="$1"; local req="$2"
    printf '%s' "$req" | "$BIN" --compact > "$TMP/$name.out" 2>"$TMP/$name.err"
}

check() { # check NAME PY_CONDITION_FILE
    local name="$1"; local condfile="$2"
    if python3 - "$TMP/$name.out" "$condfile" <<'PY'
import json, sys
out_path, cond_path = sys.argv[1], sys.argv[2]
with open(out_path) as f:
    resp = json.load(f)
with open(cond_path) as f:
    cond = f.read()
# Expose `r` (response) to the condition expression/block.
ns = {"r": resp, "json": json}
try:
    exec(cond, ns, ns)
    ok = bool(ns.get("ok", False))
except Exception as exc:  # any exception = failure
    print("condition raised:", exc)
    ok = False
sys.exit(0 if ok else 1)
PY
    then
        echo "PASS $name"; PASS=$((PASS+1))
    else
        echo "FAIL $name (response: $(cat "$TMP/$name.out" | head -c 500))"
        FAIL=$((FAIL+1))
    fi
}

mkdir -p "$TMP/cond"

# 1) C5: min-fill width 2, decomposition valid, exact optimum 2.
cat > "$TMP/cond/c5" <<'PY'
assert r["ok"] is True
d = r["data"]
assert d["n"] == 5 and d["m"] == 5
el = d["elimination"]
assert el["heuristic"] == "min-fill"
assert el["heuristic_width"] == 2
assert len(el["order"]) == 5
td = d["tree_decomposition"]
assert td["width"] == 2
v = d["verification"]
assert v["passed"] is True, v["errors"]
assert v["edges_covered"] == 5 and v["edges_total"] == 5
assert v["tree_connected"] is True
assert d["elimination_self_check"]["passed"] is True
assert d["exact"]["feasible"] is True
assert d["exact"]["optimal_treewidth"] == 2
assert d["comparison"]["heuristic_is_optimal"] is True
ok = True
PY
run c5 '{"graph":{"vertices":["0","1","2","3","4"],"edges":[[0,1],[1,2],[2,3],[3,4],[4,0]]}}'
check c5 "$TMP/cond/c5"

# 2) K5: width 4, both heuristic and exact.
cat > "$TMP/cond/k5" <<'PY'
d = r["data"]
assert d["elimination"]["heuristic_width"] == 4
assert d["tree_decomposition"]["width"] == 4
assert d["verification"]["passed"] is True
assert d["exact"]["optimal_treewidth"] == 4
# biggest bag has all 5 vertices
sizes = sorted(b["size"] for b in d["tree_decomposition"]["bags"])
assert sizes[-1] == 5
ok = True
PY
run k5 "$(cat "$ROOT/examples/request_clique5.json")"
check k5 "$TMP/cond/k5"

# 3) Disconnected: triangle + path -> two component roots joined into one tree.
cat > "$TMP/cond/disc" <<'PY'
d = r["data"]
assert d["n"] == 6
td = d["tree_decomposition"]
assert td["component_roots"] == 2
assert len(td["root_join_edges"]) == 1
assert d["verification"]["passed"] is True
assert d["verification"]["tree_connected"] is True
assert d["elimination"]["heuristic_width"] == 2
assert d["exact"]["optimal_treewidth"] == 2
ok = True
PY
run disc "$(cat "$ROOT/examples/request_disconnected.json")"
check disc "$TMP/cond/disc"

# 4) Known non-optimal case: heuristic 5, optimum 4, gap 1, flagged.
cat > "$TMP/cond/nonopt" <<'PY'
d = r["data"]
hw = d["elimination"]["heuristic_width"]
opt = d["exact"]["optimal_treewidth"]
assert hw == 5, hw
assert opt == 4, opt
assert d["comparison"]["heuristic_is_optimal"] is False
assert d["comparison"]["gap"] == 1
assert d["verification"]["passed"] is True
# wording guard: the heuristic width field is never named "optimal"
assert "heuristic_width" in d["elimination"]
assert "note" in d["elimination"]
ok = True
PY
run nonopt "$(cat "$ROOT/examples/request_heuristic_nonoptimal.json")"
check nonopt "$TMP/cond/nonopt"

# 5) Batch request: array in, array out; naive vs exact agree on C5.
cat > "$TMP/cond/batch" <<'PY'
assert isinstance(r, list) and len(r) == 4
assert all(x["ok"] for x in r), [x.get("error") for x in r if not x["ok"]]
naive = r[0]["data"]["result"]
exact = r[1]["data"]["result"]
assert naive["feasible"] and exact["feasible"]
assert naive["optimal_treewidth"] == exact["optimal_treewidth"] == 2
assert naive["permutations_checked"] == 120
gen = r[2]["data"]["graph"]
assert len(gen["vertices"]) == 8
solve = r[3]["data"]
assert solve["verification"]["passed"] is True
ok = True
PY
run batch "$(cat "$ROOT/examples/request_batch.json")"
check batch "$TMP/cond/batch"

# 6) Naive reference on an empty graph and single vertex.
cat > "$TMP/cond/tiny" <<'PY'
assert r[0]["data"]["result"]["optimal_treewidth"] == -1
assert r[1]["data"]["result"]["optimal_treewidth"] == 0
assert r[0]["data"]["result"]["permutations_checked"] == 1
ok = True
PY
run tiny '[{"action":"naive","graph":{"n":0,"edges":[]}},{"action":"naive","graph":{"n":1,"edges":[]}}]'
check tiny "$TMP/cond/tiny"

# 7) Error handling: malformed JSON, self-loop, over-heuristic-limit, bad
#    heuristic. All must come back as an ok:false error envelope.
printf '%s' '{"graph":{"n":2,"edges":[[0,0]]}}' | "$BIN" --compact > "$TMP/selfloop.out"
printf '%s' '{bad json' | "$BIN" --compact > "$TMP/malformed.out"
printf '%s' '{"graph":{"n":101,"edges":[]}}' | "$BIN" --compact > "$TMP/oversize.out"
printf '%s' '{"graph":{"n":3,"edges":[[0,1]]},"heuristic":"bogus"}' | "$BIN" --compact > "$TMP/badh.out"
python3 - "$TMP" <<'PY'
import json, sys
tmp = sys.argv[1]
def load(p):
    with open(f"{tmp}/{p}.out") as f: return json.load(f)
sl, mf, ov, bh = load("selfloop"), load("malformed"), load("oversize"), load("badh")
assert sl["ok"] is False and "self-loop" in sl["error"]
assert mf["ok"] is False and "parse" in mf["error"].lower()
assert ov["ok"] is False and "limit" in ov["error"]
assert bh["ok"] is False and "unknown heuristic" in bh["error"]
print("error-handling assertions ok")
PY
if [[ $? -eq 0 ]]; then echo "PASS error-handling"; PASS=$((PASS+1)); else echo "FAIL error-handling"; FAIL=$((FAIL+1)); fi

# 8) --input file mode matches stdin mode (timing fields are stripped).
"$BIN" --input "$ROOT/examples/request_cycle5.json" --compact > "$TMP/file.out"
printf '%s' "$(cat "$ROOT/examples/request_cycle5.json")" | "$BIN" --compact > "$TMP/stdin.out"
python3 -c '
import json, sys
def scrub(o):
    if isinstance(o, dict):
        return {k: scrub(v) for k, v in o.items()
                if k not in ("wall_ms", "elapsed_ms")}
    if isinstance(o, list):
        return [scrub(x) for x in o]
    return o
a = scrub(json.load(open(sys.argv[1])))
b = scrub(json.load(open(sys.argv[2])))
assert a == b
assert a["data"]["verification"]["passed"]
' "$TMP/file.out" "$TMP/stdin.out"
if [[ $? -eq 0 ]]; then echo "PASS file-and-stdin parity"; PASS=$((PASS+1)); else echo "FAIL file-and-stdin parity"; FAIL=$((FAIL+1)); fi

# 9) Heuristic vs optimum: never below optimum on many random graphs, and the
#    known counterexample is forced into the sample so a real gap is observed.
python3 - "$BIN" "$ROOT/examples/request_heuristic_nonoptimal.json" <<'PY'
import json, random, subprocess, sys
binp, forced_path = sys.argv[1], sys.argv[2]
random.seed(2026)

def ask(req):
    out = subprocess.run([binp, "--compact"], input=json.dumps(req),
                         capture_output=True, text=True)
    return json.loads(out.stdout)

gaps = 0
forced = json.load(open(forced_path))
r = ask(forced)
assert r["ok"], r
d = r["data"]
assert d["verification"]["passed"], d["verification"]["errors"]
assert d["comparison"]["gap"] == 1
gaps += 1

for t in range(60):
    n = random.randint(1, 10)
    p = random.uniform(0.0, 0.8)
    edges = [[i, j] for i in range(n) for j in range(i + 1, n)
             if random.random() < p]
    r = ask({"graph": {"n": n, "edges": edges}, "exact_limit": 10})
    assert r["ok"], r
    d = r["data"]
    assert d["verification"]["passed"], d["verification"]["errors"]
    hw = d["elimination"]["heuristic_width"]
    opt = d["exact"]["optimal_treewidth"]
    assert hw >= opt, (n, edges, hw, opt)
    if hw > opt:
        gaps += 1
print(f"random-crosscheck ok ({gaps} suboptimal cases observed)")
PY
if [[ $? -eq 0 ]]; then echo "PASS random-crosscheck"; PASS=$((PASS+1)); else echo "FAIL random-crosscheck"; FAIL=$((FAIL+1)); fi

echo
echo "E2E: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
