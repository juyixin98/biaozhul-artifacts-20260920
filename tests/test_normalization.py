"""Tests for name normalization, aliases and cross-language canonicalization."""

from __future__ import annotations

import pytest

from entity_resolution.errors import InvalidRequestError
from entity_resolution.normalization import AliasIndex, normalize_name


def test_case_punctuation_and_legal_suffix_fold() -> None:
    assert (
        normalize_name("Acme Trading Ltd.").canonical
        == normalize_name("ACME TRADING LIMITED").canonical
        == "acme trading"
    )


def test_legal_suffix_is_whole_word_only() -> None:
    # "Co" inside a real name must not be stripped (no substring damage).
    assert normalize_name("Atlas Copco").canonical == "atlas copco"


def test_cyrillic_transliteration_bridges_script() -> None:
    assert normalize_name("Газпром").canonical == normalize_name("Gazprom").canonical


def test_cjk_variant_and_suffix_longest_match() -> None:
    # traditional variant + full legal suffix folds to the simplified core
    assert normalize_name("国際銀行有限公司").canonical == "国际银行"


@pytest.mark.parametrize(
    "bad",
    ["", "   ", "\t\n"],
)
def test_empty_name_is_input_error(bad: str) -> None:
    with pytest.raises(InvalidRequestError) as exc:
        normalize_name(bad)
    assert exc.value.category == "INPUT_ERROR"


def test_alias_group_bridges_distinct_language_names() -> None:
    index = AliasIndex({"Gazprom": ["Газпром", "GAZPROM"]})
    assert index.same_alias_group(
        normalize_name("Газпром"), normalize_name("Gazprom")
    )


def test_alias_assigned_twice_is_input_error() -> None:
    with pytest.raises(InvalidRequestError) as exc:
        AliasIndex({"Gazprom": ["X"] , "Rosneft": ["X"]})
    assert exc.value.category == "INPUT_ERROR"
    assert exc.value.details["alias"] == normalize_name("X").canonical


def test_different_names_without_alias_are_not_bridged() -> None:
    index = AliasIndex({"Gazprom": ["Газпром"]})
    assert not index.same_alias_group(
        normalize_name("Gazprom"), normalize_name("Rosneft")
    )
