"""合成夹具生成器测试。"""

from __future__ import annotations

import pytest

from app.corpus.fixtures import (
    ALL_FIXTURES,
    get_fixture,
    random_corpus,
)

pytestmark = pytest.mark.unit


def test_random_corpus_is_deterministic_and_sorted_unique() -> None:
    first = random_corpus(size=100, seed=123)
    second = random_corpus(size=100, seed=123)
    assert first.words == second.words
    assert len(first.words) == len(set(first.words))
    assert list(first.words) == sorted(first.words)


def test_random_corpus_different_seeds_differ() -> None:
    assert random_corpus(size=100, seed=1).words != random_corpus(size=100, seed=2).words


def test_random_corpus_respects_size_cap() -> None:
    # 字母表 a-h、长度 1..2，可行词数 = 8 + 8^2 = 72；请求远超上限不应死循环。
    fixture = random_corpus(size=10_000, max_word_length=2, seed=5)
    assert len(fixture.words) == 72


def test_random_corpus_can_include_empty_word() -> None:
    fixture = random_corpus(size=50, seed=9, allow_empty_word=True)
    assert fixture.words[0] == ""


def test_random_corpus_rejects_negative_arguments() -> None:
    with pytest.raises(ValueError):
        random_corpus(size=-1)
    with pytest.raises(ValueError):
        random_corpus(max_word_length=-1)


@pytest.mark.parametrize("fixture", ALL_FIXTURES)
def test_all_named_fixtures_resolvable(fixture) -> None:
    assert get_fixture(fixture.name) is fixture
