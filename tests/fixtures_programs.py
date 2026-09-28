"""Hand-authored synthetic fixtures.

These are small, fully understood programs whose closures were computed by
hand.  Tests assert these *explicit* expected sets (an independent oracle
in addition to the cross-check against the separate naive evaluator), so
reference answers are never generated solely by the engine under test.
"""

# ---------------------------------------------------------------------------
# Family tree: ancestor via two rules (transitive closure, positive recursion)
# ---------------------------------------------------------------------------

ANCESTOR_PROGRAM = """
% family: ann -> bob -> cy -> dan ; eve is ann's parent ; ben sibling branch
parent(eve, ann).
parent(ann, bob).
parent(bob, cy).
parent(cy, dan).
parent(ann, ben).

ancestor(X, Y) :- parent(X, Y).
ancestor(X, Y) :- parent(X, Z), ancestor(Z, Y).
"""

# Hand-computed closure of ancestor/2.
ANCESTOR_EXPECTED = {
    ("eve", "ann"), ("eve", "bob"), ("eve", "cy"), ("eve", "ben"), ("eve", "dan"),
    ("ann", "bob"), ("ann", "cy"), ("ann", "ben"), ("ann", "dan"),
    ("bob", "cy"), ("bob", "dan"),
    ("cy", "dan"),
}

# Same rules in the opposite order and with body literals reordered; the
# closure must be identical (rule-order independence of the fixpoint).
ANCESTOR_PROGRAM_REORDERED = """
parent(eve, ann).
parent(ann, bob).
parent(bob, cy).
parent(cy, dan).
parent(ann, ben).

ancestor(X, Y) :- ancestor(Z, Y), parent(X, Z).
ancestor(X, Y) :- parent(X, Y).
"""

ANCESTOR_GOAL_ANN = "ancestor(ann, X)"
ANCESTOR_ANN_BINDINGS = [{"X": x} for x in ("ben", "bob", "cy", "dan")]

# ---------------------------------------------------------------------------
# Stratified negation: sinks (no outgoing edge) and nodes unreachable from a
# ---------------------------------------------------------------------------

GRAPH_PROGRAM = """
edge(a, b).
edge(b, c).
edge(c, c).
node(a).
node(b).
node(c).
node(d).

reachable(X) :- edge(a, X).
reachable(X) :- reachable(Y), edge(Y, X).
sink(X) :- node(X), NOT edge(X, _).
unreachable(X) :- node(X), NOT reachable(X).
"""

REACHABLE_EXPECTED = {("b",), ("c",)}
SINK_EXPECTED = {("d",)}
UNREACHABLE_EXPECTED = {("a",), ("d",)}

# A rule depending on negation at a *higher* stratum, itself used further up.
DIFF_STRATA_PROGRAM = """
edge(a, b). edge(b, a). node(a). node(b). node(c).
sink(X) :- node(X), NOT edge(X, _).
sink_not_sink_pair(X, Y) :- sink(X), node(Y), NOT sink(Y).
"""
SINK2_EXPECTED = {("c",)}
PAIR_EXPECTED = {("c", "a"), ("c", "b")}

# ---------------------------------------------------------------------------
# Compile-time rejections (category -> one representative source)
# ---------------------------------------------------------------------------

INVALID_PROGRAMS = {
    "unsafe_head": "p(X) :- q(Y).",
    "unsafe_negation": "p(X) :- q(X), NOT r(Y).",
    "unsafe_comparison": "p(X) :- q(X), X < Y.",
    "negation_cycle": (
        "p(X) :- q(X), NOT r(X).\n"
        "r(X) :- p(X).\n"
        "q(a).\n"
    ),
    "arity_mismatch": "p(a).\np(a, b).",
}

PARSE_INVALID = ["p(a :- q(a).", "p(a) q(a).", "p(X).", "p(!)."]

# ---------------------------------------------------------------------------
# Comparisons
# ---------------------------------------------------------------------------

COMPARISON_PROGRAM = """
score(ann, 90).
score(bob, 65).
score(cy, 60).
passed(Name) :- score(Name, S), S >= 65.
failed(Name) :- score(Name, S), S < 65.
"""
PASSED_EXPECTED = {("ann",), ("bob",)}
FAILED_EXPECTED = {("cy",)}
