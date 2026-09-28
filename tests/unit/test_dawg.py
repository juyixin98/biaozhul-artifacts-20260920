"""DAWG 挖掘内核单元测试。

覆盖验证材料：共享后缀、词为另一词前缀、重复词、空集合；并与
:mod:`app.core.trie.ReferenceTrie`（独立实现）比对接受语言与前缀计数；
最小性用 tests.oracle 的独立划分细化预言机断言具体状态数。
"""

from __future__ import annotations

import itertools
import random

import pytest

from app.core.dawg import DawgBuilder, build_dawg
from app.core.trie import ReferenceTrie
from app.corpus.errors import UnorderedCorpusError
from tests.oracle import minimal_live_state_count

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------
# 具体构建结果
# --------------------------------------------------------------------------


def test_empty_collection_single_nonterminal_root() -> None:
    dawg = build_dawg([])
    assert dawg.all_state_ids() == [0]
    assert dawg.states[0].final is False
    assert dawg.total_words() == 0
    assert dawg.contains("") is False
    assert dawg.prefix_count("") == 0


def test_shared_suffix_concrete_counts() -> None:
    words = ["cat", "cats", "dog", "dogs", "walk", "walks"]
    dawg = build_dawg(words)
    assert len(dawg.states) == 10
    assert dawg.stats.merge_count == 4
    assert dawg.stats.raw_states_created == 14
    assert dawg.total_words() == 6
    # 三个词根共享同一个终结状态，"s" 共享同一个终结后缀。
    for word in words:
        assert dawg.contains(word) is True
    assert dawg.prefix_count("cat") == 2
    assert dawg.prefix_count("dogs") == 1
    assert dawg.prefix_count("wa") == 2
    assert dawg.prefix_count("z") == 0


def test_word_being_prefix_of_another() -> None:
    words = ["be", "bee", "been", "beer", "bees"]
    dawg = build_dawg(words)
    assert len(dawg.states) == 5
    # "be" 自身是词，同时是其余四个词的前缀。
    assert dawg.contains("be") is True
    assert dawg.prefix_count("be") == 5
    # bee/been/beer/bees 共 4 个以 "bee" 为前缀。
    assert dawg.prefix_count("bee") == 4
    # "beer" 是词但不是任何更长词的前缀（除自身）。
    assert dawg.prefix_count("beer") == 1
    assert dawg.contains("beeer") is False


def test_duplicate_words_are_skipped_not_double_counted() -> None:
    builder = DawgBuilder()
    results = [builder.add(w) for w in ("able", "able", "ask", "ask", "ask", "blue")]
    assert results == [True, False, True, False, False, True]
    dawg = builder.build()
    assert dawg.total_words() == 3
    assert dawg.stats.duplicate_words == 3
    assert dawg.stats.accepted_words == 3
    assert dawg.prefix_count("") == 3
    assert dawg.prefix_count("able") == 1


def test_empty_word_accepted_marks_root_final() -> None:
    dawg = build_dawg(["", "a", "ab"])
    assert dawg.states[0].final is True
    assert dawg.contains("") is True
    assert dawg.contains("a") is True
    assert dawg.contains("ab") is True
    assert dawg.contains("ac") is False
    # 空前缀计数包含空词本身。
    assert dawg.prefix_count("") == 3


def test_only_empty_word() -> None:
    dawg = build_dawg([""])
    assert len(dawg.states) == 1
    assert dawg.states[0].final is True
    assert dawg.total_words() == 1


# --------------------------------------------------------------------------
# 有序输入契约（内核防御性校验）
# --------------------------------------------------------------------------


def test_builder_rejects_out_of_order_input() -> None:
    builder = DawgBuilder()
    builder.add("b")
    with pytest.raises(UnorderedCorpusError) as exc:
        builder.add("a")
    assert exc.value.error_code == "unordered_corpus"


def test_builder_rejects_non_string() -> None:
    builder = DawgBuilder()
    with pytest.raises(TypeError):
        builder.add(1)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# 最小性（独立预言机，不共享被测实现代码）
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "words",
    [
        [],
        [""],
        ["cat", "cats", "dog", "dogs"],
        ["cat", "cats", "dog", "dogs", "walk", "walks"],
        ["be", "bee", "been", "beer", "bees"],
        ["0", "01", "011", "10", "110"],
        ["", "a", "ab"],
    ],
)
def test_state_count_equals_independent_minimum(words: list[str]) -> None:
    ordered = sorted(set(words))
    dawg = build_dawg(ordered)
    assert len(dawg.states) == minimal_live_state_count(ordered)


def test_dense_binary_language_is_five_states() -> None:
    # {0,1} 上长度 0..4 的全部串：最小存活 DFA 恰为 5 个状态。
    words = sorted(
        {"".join(p) for r in range(5) for p in itertools.product("01", repeat=r)}
    )
    dawg = build_dawg(words)
    assert len(dawg.states) == 5
    assert minimal_live_state_count(words) == 5


# --------------------------------------------------------------------------
# 与独立参考 Trie 的差分（接受语言 + 前缀计数）
# --------------------------------------------------------------------------


def _enumerate_domain(words: list[str], extra_depth: int = 1) -> set[str]:
    alphabet = sorted(set("".join(words)))
    max_len = max((len(w) for w in words), default=0) + extra_depth
    domain: set[str] = {""}
    for radius in range(1, max_len + 1):
        for combo in itertools.product(alphabet, repeat=radius):
            domain.add("".join(combo))
    return domain


@pytest.mark.parametrize("seed", [1, 7, 42, 20260927])
def test_differential_against_reference_trie_random(seed: int) -> None:
    rng = random.Random(seed)
    words_set = {
        "".join(rng.choice("abc") for _ in range(rng.randint(1, 6)))
        for _ in range(80)
    }
    words = sorted(words_set)
    dawg = build_dawg(words)
    trie = ReferenceTrie(words)
    domain = _enumerate_domain(words)
    language_diff = [w for w in domain if dawg.contains(w) != trie.contains(w)]
    count_diff = [w for w in domain if dawg.prefix_count(w) != trie.prefix_count(w)]
    assert language_diff == []
    assert count_diff == []


def test_differential_shared_prefixes_full_enumeration() -> None:
    words = ["a", "ab", "abc", "abcd", "abcde", "abcef", "b", "bc", "bcd"]
    dawg = build_dawg(words)
    trie = ReferenceTrie(words)
    domain = _enumerate_domain(words)
    assert [w for w in domain if dawg.contains(w) != trie.contains(w)] == []
    assert [w for w in domain if dawg.prefix_count(w) != trie.prefix_count(w)] == []


def test_reachable_set_matches_edges_and_no_dangling_in_memory() -> None:
    dawg = build_dawg(["cat", "cats", "dog", "dogs"])
    targets = {target for _, _, target in dawg.iter_edges()}
    reachable = dawg.reachable_state_ids()
    assert targets <= reachable
    assert set(dawg.all_state_ids()) == reachable
