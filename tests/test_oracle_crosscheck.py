"""Cross-checks against the INDEPENDENT finite-model oracle.

The oracle (``app.oracle``) shares no inference code with the kernel -- it
compiles the ontology to propositional clauses and brute-force enumerates
every model. These tests therefore validate the kernel against a reference
that was not produced by the implementation under test:

* every shipped fixture is checked node-by-node;
* a deterministic random generator builds hundreds of ontologies and the two
  implementations must agree on unsatisfiability, consistency, all named
  subsumptions and every instance's entailed types.
"""
from __future__ import annotations

import random

import pytest

from app.kernel import reason
from app.language import parse_ontology
from app.oracle import evaluate

from .conftest import load_fixture

FIXTURE_NAMES = [
    "multi_inheritance",
    "equivalence_ring",
    "intersection_disjoint_conflict",
    "two_assertions_conflict",
]

NAMES = ["A", "B", "C", "D", "E"]


@pytest.mark.parametrize("name", FIXTURE_NAMES)
@pytest.mark.integration
def test_fixture_kernel_matches_oracle(name: str):
    onto = parse_ontology(load_fixture(name))
    kernel = reason(onto, engine_version="test")
    oracle = evaluate(onto)

    assert set(kernel.unsatisfiable_nodes) == set(oracle.unsatisfiable_nodes)
    assert kernel.consistent is oracle.consistent

    # every named subsumption pair must agree
    for sub in oracle.subsumptions:
        assert kernel.is_subclass(*sub), f"kernel misses subsumption {sub}"
    present = set(kernel.classes)
    for a in present:
        for b in present:
            if a != b:
                assert kernel.is_subclass(a, b) == ((a, b) in oracle.subsumptions)

    # instance entailed types agree (only meaningful while consistent)
    if kernel.consistent:
        kernel_map = {i.instance: set(i.entailed_types)
                      for i in kernel.instances}
        for inst, forced in oracle.instance_entailed_types.items():
            assert kernel_map.get(inst, set()) == set(forced)


@pytest.mark.integration
def test_oracle_finds_expected_number_of_models():
    # A trivial ontology (no axioms beyond two named classes) has all 2^2
    # class truth patterns for a single hypothetical individual.
    onto = parse_ontology({"axioms": []})
    verdict = evaluate(onto)
    # two classes appear only when present in signature; empty ontology has
    # zero variables -> exactly one (empty) model
    assert verdict.variable_count == 0
    assert verdict.model_count == 1


@pytest.mark.integration
def test_oracle_detects_inconsistent_instance_independently():
    onto = parse_ontology({"axioms": [
        {"disjoint": ["A", "B"]},
        {"instance": "i", "class": "A"},
        {"instance": "i", "class": "B"},
    ]})
    verdict = evaluate(onto)
    assert verdict.consistent is False
    assert set(verdict.unsatisfiable_classes) == set()


# ---------------------------------------------------------------------------
# Deterministic differential fuzzing
# ---------------------------------------------------------------------------

def _rand_expr(rng: random.Random, depth: int = 0):
    if depth >= 2 or rng.random() < 0.6:
        return rng.choice(NAMES)
    return {"intersection": rng.sample(NAMES, rng.choice([2, 3]))}


def _gen_ontology(rng: random.Random) -> dict:
    axioms = []
    for _ in range(rng.randint(1, 7)):
        pick = rng.random()
        if pick < 0.4:
            axioms.append({"sub": _rand_expr(rng), "super": _rand_expr(rng)})
        elif pick < 0.55:
            axioms.append(
                {"equivalent": [rng.choice(NAMES), _rand_expr(rng)]}
            )
        elif pick < 0.78:
            axioms.append(
                {"disjoint": [_rand_expr(rng), _rand_expr(rng)]}
            )
        else:
            axioms.append({
                "instance": f"i{rng.randint(0, 2)}",
                "class": _rand_expr(rng),
            })
    return {"axioms": axioms}


@pytest.mark.integration
@pytest.mark.parametrize("seed", range(30))
def test_differential_random_ontologies(seed: int):
    rng = random.Random(seed * 1009 + 13)
    for _ in range(40):  # 30 * 40 = 1200 ontologies
        payload = _gen_ontology(rng)
        onto = parse_ontology(payload)
        kernel = reason(onto, engine_version="fuzz")
        oracle = evaluate(onto)

        assert set(kernel.unsatisfiable_nodes) == set(oracle.unsatisfiable_nodes), (
            f"unsatisfiability mismatch for {payload}"
        )
        assert kernel.consistent is oracle.consistent, payload

        for a in NAMES:
            for b in NAMES:
                if a != b:
                    assert kernel.is_subclass(a, b) == (
                        (a, b) in oracle.subsumptions
                    ), (a, b, payload)

        if kernel.consistent:
            kernel_map = {i.instance: set(i.entailed_types)
                          for i in kernel.instances}
            for inst, forced in oracle.instance_entailed_types.items():
                assert kernel_map.get(inst, set()) == set(forced), (
                    inst, payload
                )
