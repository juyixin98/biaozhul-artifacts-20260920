"""语料规范层：排序契约（契约 2）、空词规则（契约 3）、重复词处理。"""

import pytest

from minidfa import (
    DuplicateWordError,
    InvalidWordError,
    UnsortedInputError,
    build_minimal_dfa,
)
from minidfa.corpus import InputMode, prepare_corpus


class TestStrictMode:
    def test_unsorted_rejected_with_position(self):
        with pytest.raises(UnsortedInputError) as exc_info:
            prepare_corpus(["b", "a"], InputMode.STRICT)
        assert exc_info.value.category == "UNSORTED_INPUT"
        assert exc_info.value.detail["index"] == 1
        assert exc_info.value.detail["previous"] == "b"
        assert exc_info.value.detail["current"] == "a"

    def test_duplicate_rejected_with_position(self):
        with pytest.raises(DuplicateWordError) as exc_info:
            prepare_corpus(["a", "b", "b"], InputMode.STRICT)
        assert exc_info.value.category == "DUPLICATE_WORD"
        assert exc_info.value.detail == {"index": 2, "word": "b"}

    def test_non_string_rejected(self):
        with pytest.raises(InvalidWordError) as exc_info:
            prepare_corpus(["a", 42], InputMode.STRICT)
        assert exc_info.value.category == "INVALID_WORD"

    def test_builder_also_enforces_order(self):
        """内核不信任绕过语料层的直接调用。"""
        with pytest.raises(UnsortedInputError):
            build_minimal_dfa(["z", "a"])


class TestNormalizeMode:
    def test_sorts_and_dedupes(self):
        spec = prepare_corpus(["b", "a", "b", "c", "a"], InputMode.NORMALIZE)
        assert spec.words == ("a", "b", "c")

    def test_fingerprint_stable_and_order_sensitive_content(self):
        a = prepare_corpus(["b", "a"], InputMode.NORMALIZE)
        b = prepare_corpus(["a", "b"], InputMode.STRICT)
        c = prepare_corpus(["a", "b", "c"], InputMode.STRICT)
        assert a.fingerprint == b.fingerprint  # 同一规范化语料指纹一致
        assert a.fingerprint != c.fingerprint


class TestEmptyWordRule:
    """固定规则：空词合法、字典序最小、至多一次、体现为初始状态终结。"""

    def test_empty_word_accepted_when_present(self):
        dfa = build_minimal_dfa(["", "a", "ab"])
        assert dfa.contains("")
        assert dfa.contains("a")
        assert not dfa.contains("b")
        assert dfa.prefix_count("") == 3

    def test_empty_word_absent_means_rejected(self):
        dfa = build_minimal_dfa(["a"])
        assert not dfa.contains("")
        assert dfa.prefix_count("") == 1

    def test_duplicate_empty_word_rejected(self):
        with pytest.raises(DuplicateWordError):
            prepare_corpus(["", ""], InputMode.STRICT)

    def test_empty_word_must_sort_first(self):
        with pytest.raises(UnsortedInputError):
            prepare_corpus(["a", ""], InputMode.STRICT)


class TestEmptyCorpus:
    def test_empty_corpus_builds_single_state(self):
        dfa = build_minimal_dfa([])
        assert dfa.state_count == 1
        assert len(dfa) == 0
        assert not dfa.contains("")
        assert not dfa.contains("anything")
        assert dfa.prefix_count("") == 0
        assert list(dfa.iter_words()) == []
