"""Cross-checks against the INDEPENDENT brute-force oracle.

These tests exhaustively enumerate every path of the tiny synthetic
fixtures with a separate DFS implementation (``tests/oracle.py``) that
shares no code with the kernel, then compare the SUT's k-best outputs
element-by-element.  They also verify that running the two transducers
sequentially (left, then right, joined on the middle string) agrees with
the precomposed pipeline.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from wfst_service.corpus import build_corpus, load_corpus_document
from wfst_service.core.compose import compose
from wfst_service.core.search import kbest
from tests.oracle import (
    assert_topk_exact,
    compose_relations,
    enumerate_relation,
    raw_from_spec_body,
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "corpora"


@pytest.fixture(scope="module")
def demo() -> dict:
    return json.loads((FIXTURE / "demo_corpus.json").read_text())


@pytest.fixture(scope="module")
def built(demo: dict):
    return build_corpus(load_corpus_document(FIXTURE / "demo_corpus.json"))


def _raw(demo: dict, name: str):
    body = next(t for t in demo["transducers"] if t["name"] == name)
    return raw_from_spec_body(body)


def _sut_pairs(result) -> list[tuple[str, float]]:
    return [(o.output, o.cost) for o in result.outputs]


@pytest.mark.oracle
@pytest.mark.parametrize("text", ["kat", "ca", "dog", "katz"])
def test_single_transducer_matches_exhaustive_oracle(
    demo: dict, built, text: str
) -> None:
    raw = _raw(demo, "char_correction")
    enumeration = enumerate_relation(raw, text)
    assert enumeration.nonneg_weights

    result, trace = kbest(
        built.fsts["char_correction"], text, 8, run_id=f"oracle-single-{text}"
    )
    # The trace must expose the computation steps and the verdict.
    joined = "\n".join(trace.as_lines())
    assert f"input={text!r}" in joined
    assert trace.verdict.startswith("complete")

    assert_topk_exact(enumeration, _sut_pairs(result))


@pytest.mark.oracle
@pytest.mark.parametrize("text", ["kat", "cats", "ca"])
def test_composed_pipeline_matches_independent_composition_oracle(
    demo: dict, built, text: str
) -> None:
    # Independent ground truth: enumerate the left relation for the text,
    # then for every middle string enumerate the right relation from
    # scratch -- relational composition without using any SUT composition
    # or search code.
    left_raw = _raw(demo, "char_correction")
    right_raw = _raw(demo, "morphology")
    left_enum = enumerate_relation(left_raw, text)
    oracle_composed = compose_relations(left_enum, right_raw)

    composed, _ = compose(
        built.fsts["char_correction"],
        built.fsts["morphology"],
        "correct_then_morph",
    )
    result, trace = kbest(composed, text, 8, run_id=f"oracle-comp-{text}")
    assert trace.verdict.startswith("complete")
    assert_topk_exact(oracle_composed, _sut_pairs(result))


@pytest.mark.oracle
def test_sequential_execution_agrees_with_composition_and_oracle(
    demo: dict, built
) -> None:
    """Composition and sequential (stage-by-stage) execution agree.

    Sequential execution: enumerate middle strings from the left stage
    (bounded at a proved-sufficient cost threshold), query the right
    stage for each, sum costs and minimise.  Cross-checked against both
    the precomposed SUT pipeline and the independent oracle.
    """
    text = "kat"
    threshold = 1.0  # middle strings more expensive than this are pruned

    left_result, _ = kbest(
        built.fsts["char_correction"], text, 300, run_id="seq-left"
    )
    middles = [
        (o.output, o.cost)
        for o in left_result.outputs
        if o.cost <= threshold
    ]
    # Proof that the (cost-bounded) middle enumeration is sufficient:
    # either the language was exhausted, or the next middle already
    # costs strictly more than the threshold.
    assert left_result.complete or (
        left_result.outputs and left_result.outputs[-1].cost > threshold
    )
    assert {m for m, _ in middles}  # at least the identity middle "kat"

    sequential: dict[str, float] = {}
    for middle, left_cost in middles:
        right_result, _ = kbest(
            built.fsts["morphology"], middle, 50, run_id=f"seq-right-{middle}"
        )
        for item in right_result.outputs:
            total = left_cost + item.cost
            if total <= threshold + 0.3 + 1e-9:
                prev = sequential.get(item.output)
                if prev is None or total < prev:
                    sequential[item.output] = total

    # 1) Sequential vs precomposed SUT pipeline.
    composed, _ = compose(
        built.fsts["char_correction"],
        built.fsts["morphology"],
        "correct_then_morph",
    )
    comp_result, _ = kbest(composed, text, 50, run_id="seq-composed")
    comp_pairs = {
        o.output: o.cost
        for o in comp_result.outputs
        if o.cost <= threshold + 0.3 + 1e-9
    }
    assert set(sequential) == set(comp_pairs)
    for output, cost in sequential.items():
        assert cost == pytest.approx(comp_pairs[output], abs=1e-9)

    # 2) Both vs the independent oracle on the same cost window.
    left_raw = _raw(demo, "char_correction")
    right_raw = _raw(demo, "morphology")
    truth = compose_relations(
        enumerate_relation(left_raw, text), right_raw
    ).best
    for output, cost in sequential.items():
        assert output in truth
        assert cost == pytest.approx(truth[output], abs=1e-9)


@pytest.mark.oracle
def test_ambiguous_outputs_ranked_like_oracle(demo: dict, built) -> None:
    # The lexicon stage maps "cat" to both "cat" (0) and "feline" (1.0);
    # composed with the speller the oracle must see the same ambiguity in
    # (cost, lexicographic) order.
    text = "cat"
    # The lexicon is *expanded* by the spec layer, so build the oracle
    # view from its expanded explicit FST dump (still no search code).
    from wfst_service.corpus import fst_to_dict
    from tests.oracle import raw_from_spec_body

    expanded = fst_to_dict(built.fsts["lex_general"])
    raw_lex = raw_from_spec_body(expanded)
    truth = enumerate_relation(raw_lex, text)

    result, _ = kbest(
        built.fsts["lex_general"], text, 6, run_id="oracle-lex"
    )
    assert_topk_exact(truth, _sut_pairs(result))
    # Concrete ambiguity assertion (not just "the API works"):
    outputs = _sut_pairs(result)
    assert ("cat", 0.0) in [(o, round(c, 6)) for o, c in outputs]
    assert any(o == "feline" and abs(c - 1.0) < 1e-9 for o, c in outputs)
    assert outputs[0][0] == "cat"
