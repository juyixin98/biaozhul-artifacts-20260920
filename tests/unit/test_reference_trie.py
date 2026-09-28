"""参考 Trie 自身的朴素正确性测试。

参考实现必须先被证明是对的，才能作为差分比对的"第二意见"。这里用最直白的
集合定义断言它的成员判定与子树计数。
"""

from __future__ import annotations

import pytest

from app.core.trie import ReferenceTrie

pytestmark = pytest.mark.unit


def test_membership_matches_membership_set() -> None:
    words = ["a", "ab", "bc"]
    trie = ReferenceTrie(words)
    members = set(words)
    for candidate in ["", "a", "ab", "abc", "b", "bc", "bcd", "c"]:
        assert trie.contains(candidate) is (candidate in members)


def test_insert_dedup_and_len() -> None:
    trie = ReferenceTrie()
    assert trie.insert("x") is True
    assert trie.insert("x") is False
    assert len(trie) == 1


def test_prefix_counts_by_bruteforce() -> None:
    words = ["be", "bee", "been", "beer", "bees", "cat"]
    trie = ReferenceTrie(words)
    for prefix in ["", "b", "be", "bee", "been", "beer", "bees", "beeX", "c", "ca", "z"]:
        expected = sum(1 for w in words if w.startswith(prefix))
        assert trie.prefix_count(prefix) == expected


def test_empty_prefix_is_total_size() -> None:
    words = ["a", "ab", "abc"]
    trie = ReferenceTrie(words)
    assert trie.prefix_count("") == len(words) == 3


def test_empty_word_supported_by_reference() -> None:
    trie = ReferenceTrie(["", "a"])
    assert trie.contains("") is True
    assert trie.prefix_count("") == 2
