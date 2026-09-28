"""Unit tests: symbol normalisation."""

from __future__ import annotations

import pytest

from wfst_service.corpus.symbols import EPS, EPS_LITERAL, normalize_label, render


@pytest.mark.unit
def test_epsilon_spellings_normalise_to_empty_string() -> None:
    assert normalize_label("") == EPS
    assert normalize_label(EPS_LITERAL) == EPS
    assert render(EPS) == EPS_LITERAL


@pytest.mark.unit
def test_single_characters_pass_through() -> None:
    assert normalize_label("a") == "a"
    assert normalize_label("中") == "中"


@pytest.mark.unit
@pytest.mark.parametrize("bad", ["ab", "abc", " ", "\t"])
def test_rejected_labels(bad: str) -> None:
    with pytest.raises((TypeError, ValueError)):
        normalize_label(bad)


@pytest.mark.unit
def test_non_string_label_is_type_error() -> None:
    with pytest.raises(TypeError):
        normalize_label(3)  # type: ignore[arg-type]
