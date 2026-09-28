"""Tests for pairwise similarity: explainable evidence and hard veto."""

from __future__ import annotations

from entity_resolution.normalization import AliasIndex
from entity_resolution.similarity import (
    SimilarityConfig,
    build_candidates,
    prepare_record,
    score_pair,
)


def test_identical_name_scores_high_and_is_candidate() -> None:
    cfg = SimilarityConfig(threshold=0.6)
    a = prepare_record("a", "Acme Bank Ltd")
    b = prepare_record("b", "Acme Bank Limited")
    ps = score_pair(a, b, cfg)
    assert ps.candidate is True
    # token+char weights sum to 0.80; identical canonical core saturates that.
    assert 0.79 <= ps.score <= 0.8
    assert ps.char_similarity > 0.9


def test_same_name_with_conflicting_hard_attribute_is_vetoed() -> None:
    # "Same name != same entity": equal display name but a disagreeing hard
    # identifier must collapse the score and forbid candidacy.
    cfg = SimilarityConfig(
        threshold=0.6, hard_attribute_keys=frozenset({"reg_id"})
    )
    a = prepare_record("a", "Acme Bank", attributes={"reg_id": "111"})
    b = prepare_record("b", "Acme Bank", attributes={"reg_id": "222"})
    ps = score_pair(a, b, cfg)
    assert ps.vetoed is True
    assert ps.candidate is False
    assert ps.score == 0.0
    assert "reg_id" in ps.conflicting_attributes


def test_same_name_agreeing_hard_attribute_is_allowed() -> None:
    cfg = SimilarityConfig(
        threshold=0.6, hard_attribute_keys=frozenset({"reg_id"})
    )
    a = prepare_record("a", "Acme Bank", attributes={"reg_id": "111"})
    b = prepare_record("b", "Acme Bank Branch", attributes={"reg_id": "111"})
    ps = score_pair(a, b, cfg)
    assert ps.vetoed is False
    assert ps.same_attributes == {"reg_id": "111"}


def test_cross_language_alias_boosts_pair() -> None:
    cfg = SimilarityConfig(threshold=0.55)
    aliases = AliasIndex({"Gazprom": ["Газпром"]})
    a = prepare_record("a", "Газпром", language="ru")
    b = prepare_record("b", "Gazprom", language="en")
    ps = score_pair(a, b, cfg, aliases)
    assert ps.alias_match is True
    assert ps.candidate is True
    # Evidence components are all bounded.
    assert 0.0 <= ps.token_overlap <= 1.0
    assert 0.0 <= ps.char_similarity <= 1.0


def test_build_candidates_scores_each_unordered_pair_once() -> None:
    cfg = SimilarityConfig(threshold=0.0)
    recs = [prepare_record(str(i), f"Name {i}") for i in range(4)]
    candidates = build_candidates(recs, cfg)
    assert len(candidates) == 6  # C(4,2)
    assert all(k[0] < k[1] for k in candidates)


def test_unrelated_names_below_threshold() -> None:
    cfg = SimilarityConfig(threshold=0.6)
    a = prepare_record("a", "Global Mining Corp")
    b = prepare_record("b", "Sunrise Bakery LLC")
    ps = score_pair(a, b, cfg)
    assert ps.candidate is False
