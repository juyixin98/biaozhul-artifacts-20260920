"""Property-style cross-check: on many small random, stratified, safe
programs the semi-naive engine and the independent naive engine must agree.

Programs are generated (not hand-picked) but expected answers still come
from the independent naive evaluator, never from the engine under test.
Generation is restricted to shapes guaranteed safe and stratifiable:
* EDB facts over a tiny domain;
* non-recursive IDB rules joining EDB predicates;
* one optional transitive-closure-style positive recursion;
* optional stratified negation of an EDB predicate.
"""

import random

import pytest

from app.engine.fixpoint import evaluate
from app.engine.naive import naive_evaluate
from app.language.compiler import compile_program
from app.language.parser import parse_program


def generate_program(rng: random.Random) -> str:
    dom = ["a", "b", "c", "d"]
    lines: list[str] = []

    # Random EDB graph edge/2.
    edges = set()
    for x in dom:
        for y in dom:
            if rng.random() < 0.25:
                edges.add((x, y))
    for x, y in sorted(edges):
        lines.append(f"edge({x}, {y}).")
    for x in dom:
        lines.append(f"node({x}).")

    # Colour EDB relation colour/2 with fixed values.
    for x in dom:
        if rng.random() < 0.6:
            lines.append(f"colour({x}, red).")
        else:
            lines.append(f"colour({x}, blue).")

    # Positive transitive closure from a fixed root 'a'.
    lines.append("reach(X) :- edge(a, X).")
    lines.append("reach(X) :- reach(Y), edge(Y, X).")

    # Stratified negation over EDB only.
    if rng.random() < 0.5:
        lines.append("nored(X) :- node(X), NOT colour(X, red).")
    # Negation depending on the recursive predicate (higher stratum).
    if rng.random() < 0.5:
        lines.append("notreach(X) :- node(X), NOT reach(X).")
    # Join + comparison.
    if rng.random() < 0.5:
        lines.append("selfloop(X) :- edge(X, Y), X = Y.")

    return "\n".join(lines) + "\n"


@pytest.mark.parametrize("seed", range(40))
def test_random_programs_engines_agree(seed):
    rng = random.Random(seed)
    src = generate_program(rng)
    compiled = compile_program(parse_program(src))
    fast = evaluate(compiled).db.rels
    slow = naive_evaluate(compiled)
    assert set(fast) == set(slow)
    for key in slow:
        assert fast[key] == slow[key], f"seed {seed} mismatch on {key}:\n{src}"


@pytest.mark.parametrize("seed", [1, 7, 23])
def test_random_program_permuted_rules_same_closure(seed):
    rng = random.Random(seed)
    src = generate_program(rng)

    def split_facts_rules(text):
        facts, rules = [], []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            (rules if ":-" in line else facts).append(line)
        return facts, rules

    facts, rules = split_facts_rules(src)
    rng.shuffle(rules)
    permuted = "\n".join(facts + rules) + "\n"

    c1 = compile_program(parse_program(src))
    c2 = compile_program(parse_program(permuted))
    r1 = evaluate(c1).db.rels
    r2 = evaluate(c2).db.rels
    assert r1 == r2
