"""与独立 Trie 参考实现交叉验证：接受语言与前缀计数。

参考结果由 tests/reference_trie.py（独立实现）产生，不由被测核心生成。
覆盖：共享后缀、词互为前缀、重复词（规范化后）、空集合、随机语料。
"""

import random
import string

import pytest

from minidfa import build_minimal_dfa
from tests.reference_trie import ReferenceTrie

# 手工选定语料：共享后缀 + 词为另一词前缀 + 单字符词
FIXED_CORPORA = [
    [],
    [""],
    ["a", "ab", "abc"],                      # 词互为前缀
    ["cat", "bat", "rat", "cats", "cat"],    # 含重复词（构建前去重）
    ["ape", "apple", "applet", "banana", "band", "bandit"],
    ["x"],                                   # 单词语料
]


def _dedupe_sorted(words):
    return sorted(set(words))


def _all_prefixes(words):
    prefixes = {""}
    for w in words:
        for i in range(1, len(w) + 1):
            prefixes.add(w[:i])
    return prefixes


@pytest.mark.parametrize("words", FIXED_CORPORA, ids=lambda ws: f"n={len(ws)}")
def test_language_and_prefix_counts_match_trie(words):
    corpus = _dedupe_sorted(words)
    dfa = build_minimal_dfa(corpus)
    trie = ReferenceTrie(corpus)

    # 接受语言完全一致
    assert sorted(dfa.iter_words()) == trie.words()
    assert len(dfa) == len(trie.words())

    # 成员查询：语料词、语料词+后缀、随机非语料词
    probes = set(corpus)
    probes |= {w + "x" for w in corpus}
    probes |= {"zzz", "q", "aa"}
    for probe in probes:
        assert dfa.contains(probe) == trie.contains(probe), f"contains({probe!r})"

    # 前缀计数：全部真实前缀 + 不存在的前缀
    for prefix in _all_prefixes(corpus) | {"zzz", "qx"}:
        assert dfa.prefix_count(prefix) == trie.prefix_count(prefix), (
            f"prefix_count({prefix!r})"
        )


def _random_corpus(rng: random.Random, size: int, alphabet: str, max_len: int):
    return sorted(
        {
            "".join(rng.choice(alphabet) for _ in range(rng.randint(1, max_len)))
            for _ in range(size)
        }
    )


@pytest.mark.parametrize("seed", [1, 7, 42, 2026])
def test_random_corpora_match_trie(seed):
    rng = random.Random(seed)
    corpus = _random_corpus(rng, size=300, alphabet="abc", max_len=8)
    dfa = build_minimal_dfa(corpus)
    trie = ReferenceTrie(corpus)

    assert sorted(dfa.iter_words()) == trie.words()

    for prefix in _all_prefixes(corpus):
        assert dfa.prefix_count(prefix) == trie.prefix_count(prefix)

    # 负例探测：随机非语料词
    for _ in range(200):
        probe = "".join(rng.choice(string.ascii_lowercase) for _ in range(rng.randint(1, 10)))
        assert dfa.contains(probe) == trie.contains(probe)


def test_minimal_dfa_never_larger_than_trie():
    """最小自动机状态数不超过 Trie 节点数（最小性的必要条件）。"""
    rng = random.Random(99)
    corpus = _random_corpus(rng, size=500, alphabet="ab", max_len=10)
    dfa = build_minimal_dfa(corpus)
    trie = ReferenceTrie(corpus)

    def trie_nodes(node, count=0):
        return 1 + sum(trie_nodes(c) for c in node.children.values())

    assert dfa.state_count <= trie_nodes(trie._root)
