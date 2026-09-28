"""Fixed-seed randomized differential testing vs the independent oracle.

Hundreds of tiny random non-negative-weight transducers (acyclic forward
arcs plus at most one positive self loop) are generated with a FIXED
seed, then their top-K outputs are compared element-by-element against
the brute-force oracle.  Only provably-sound prefixes are compared: the
K-th oracle cost must lie strictly below the oracle's truncation
frontier, so skipped machines never weaken an assertion silently.
"""

from __future__ import annotations

import random

import pytest

from wfst_service.core.fst import Arc, Fst
from wfst_service.core.search import kbest
from wfst_service.corpus.serialize import fst_to_dict

from tests.oracle import enumerate_relation, raw_from_spec_body

ALPHABET = "ab"
K = 5


def _random_machine(rng: random.Random) -> Fst | None:
    n = rng.randint(1, 3)
    finals = {rng.randrange(n): 0.0}
    arcs: list[Arc] = []
    for _ in range(rng.randint(4, 8)):
        src = rng.randrange(n)
        dst = rng.randrange(src, n)  # forward only -> no backward cycles
        weight = round(rng.choice([0.0, 0.0, 0.5, 1.0, 2.0]), 2)
        choice = rng.random()
        if choice < 0.6:
            ch = rng.choice(ALPHABET)
            arcs.append(Arc(src, dst, ch, ch, weight))
        elif choice < 0.8:
            arcs.append(Arc(src, dst, rng.choice(ALPHABET), "", weight))
        else:
            arcs.append(Arc(src, dst, "", rng.choice(ALPHABET), weight))
    if rng.random() < 0.35:
        src = rng.randrange(n)
        if rng.random() < 0.5:
            ch = rng.choice(ALPHABET)
            arcs.append(Arc(src, src, ch, ch, 1.0))
        else:
            arcs.append(Arc(src, src, "", rng.choice(ALPHABET), 1.0))

    merged: dict[tuple, float] = {}
    for arc in arcs:
        key = (arc.src, arc.dst, arc.ilabel, arc.olabel)
        merged[key] = min(merged.get(key, float("inf")), arc.cost)
    arcs = [Arc(s, d, i, o, w) for (s, d, i, o), w in merged.items()]
    try:
        return Fst.create("random", n, 0, finals, arcs)
    except Exception:  # pragma: no cover - generator keeps machines valid
        return None


@pytest.mark.oracle
def test_random_machines_match_oracle_on_sound_prefixes() -> None:
    rng = random.Random(20260928)
    compared = 0
    for trial in range(600):
        fst = _random_machine(rng)
        if fst is None:
            continue
        raw = raw_from_spec_body(fst_to_dict(fst))
        length = rng.choice((1, 2))
        text = "".join(rng.choice(ALPHABET) for _ in range(length))

        enumeration = enumerate_relation(raw, text, max_revisits=3)
        assert enumeration.nonneg_weights
        ranked = sorted(
            enumeration.best.items(), key=lambda kv: (kv[1], kv[0])
        )
        window = [
            (out, cost)
            for out, cost in ranked
            if cost < enumeration.cut_frontier - 1e-9
        ]
        if len(window) < K:
            continue  # prefix not provably complete for this random case

        expected = window[:K]
        result, _ = kbest(fst, text, K, run_id=f"rand-{trial}", budget=60_000)
        actual = [(o.output, o.cost) for o in result.outputs]
        assert len(actual) == K
        for (eo, ec), (ao, ac) in zip(expected, actual):
            assert eo == ao, f"trial {trial} {text!r}: {eo!r} != {ao!r}"
            assert ac == pytest.approx(ec, abs=1e-9)
        compared += 1

    # The fixed seed must produce a meaningful number of comparisons.
    assert compared >= 40, f"too few sound comparisons: {compared}"
