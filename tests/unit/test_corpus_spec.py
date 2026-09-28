"""语料规范层单元测试：断言具体结果与具体失败类别。"""

from __future__ import annotations

import logging

import pytest

from app.corpus.errors import (
    EmptyWordError,
    FixtureNotFoundError,
    InvalidWordError,
    UnorderedCorpusError,
)
from app.corpus.fixtures import (
    FIXTURE_DUPLICATES,
    FIXTURE_EMPTY,
    FIXTURE_PREFIX_WORDS,
    FIXTURE_SHARED_SUFFIX,
    FIXTURE_UNORDERED,
    FIXTURE_WITH_EMPTY_WORD,
    get_fixture,
)
from app.corpus.spec import CorpusSpec, accept_empty_word, check_sorted

pytestmark = pytest.mark.unit


def test_accept_empty_word_rule_is_fixed() -> None:
    # 契约 3：空词规则固定——默认拒绝，只有显式允许才接受。
    assert accept_empty_word(False) is False
    assert accept_empty_word(True) is True


def test_normalize_keeps_sorted_unique_and_counts_duplicates() -> None:
    result = CorpusSpec().normalize(["able", "able", "ask", "blue"])
    assert result.words == ["able", "ask", "blue"]
    assert result.input_count == 4
    assert result.unique_count == 3
    assert result.duplicate_count == 1
    assert result.was_sorted_by_us is False


def test_normalize_empty_collection() -> None:
    # 契约验证材料：空集合必须可用。
    result = CorpusSpec().normalize([])
    assert result.words == []
    assert result.unique_count == 0
    assert result.duplicate_count == 0


def test_empty_word_rejected_by_default_with_specific_code() -> None:
    with pytest.raises(EmptyWordError) as exc:
        CorpusSpec(allow_empty_word=False).normalize([""])
    assert exc.value.error_code == "empty_word_rejected"
    assert exc.value.http_status == 422


def test_empty_word_accepted_when_explicitly_allowed() -> None:
    result = CorpusSpec(allow_empty_word=True).normalize(["", "a", "ab"])
    assert result.words == ["", "a", "ab"]
    assert result.unique_count == 3


def test_unordered_input_rejected_with_specific_code() -> None:
    # 契约 2：不有序必须明确拒绝。
    with pytest.raises(UnorderedCorpusError) as exc:
        CorpusSpec(sort_first=False).normalize(["b", "a"])
    assert exc.value.error_code == "unordered_corpus"
    assert "位置 0" in str(exc.value)


def test_unordered_input_can_be_sorted_first() -> None:
    # 契约 2：或者显式先排序。
    result = CorpusSpec(sort_first=True).normalize(["banana", "apple", "cherry"])
    assert result.words == ["apple", "banana", "cherry"]
    assert result.was_sorted_by_us is True


def test_equal_neighbors_are_not_out_of_order() -> None:
    # 重复（相等相邻）不是逆序，不应抛错。
    check_sorted(["a", "a", "b", "b"])


def test_invalid_word_type_and_surrogate_rejected() -> None:
    with pytest.raises(InvalidWordError) as exc1:
        CorpusSpec().normalize([123])  # type: ignore[list-item]
    assert exc1.value.error_code == "invalid_word"

    surrogate = "a\ud800b"
    with pytest.raises(InvalidWordError) as exc2:
        CorpusSpec().normalize([surrogate])
    assert exc2.value.error_code == "invalid_word"


def test_non_list_input_rejected() -> None:
    with pytest.raises(InvalidWordError):
        CorpusSpec().normalize({"a": 1})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "fixture",
    [
        FIXTURE_SHARED_SUFFIX,
        FIXTURE_PREFIX_WORDS,
        FIXTURE_DUPLICATES,
        FIXTURE_EMPTY,
        FIXTURE_WITH_EMPTY_WORD,
    ],
)
def test_named_fixtures_are_self_describing(fixture) -> None:
    assert fixture.name
    assert fixture.description
    assert isinstance(fixture.words, tuple)


def test_unordered_fixture_is_actually_unordered() -> None:
    with pytest.raises(UnorderedCorpusError):
        CorpusSpec().normalize(FIXTURE_UNORDERED.as_list())


def test_get_fixture_unknown_raises_with_specific_code() -> None:
    with pytest.raises(FixtureNotFoundError) as exc:
        get_fixture("does_not_exist")
    assert exc.value.error_code == "unknown_fixture"
    assert exc.value.http_status == 404
