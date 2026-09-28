"""四条行为契约的专项、跨层测试。

每个用例显式标注契约编号，并用具体结果（而非"接口能调用"）判定。
"""

from __future__ import annotations

import pytest

from app.core.dawg import build_dawg
from app.core.state import state_key
from app.corpus.errors import EmptyWordError, UnorderedCorpusError
from app.corpus.spec import CorpusSpec
from app.index.errors import IndexIntegrityError
from app.index.repository import SQLiteIndexRepository
from app.index.service import IndexService
from app.index.validator import StoredEdge, StoredState, validate_references

pytestmark = pytest.mark.contract


# --------------------------------------------------------------------------
# 契约 1：状态等价须同时比较终结标记和转移，不能只按子节点数量合并。
# --------------------------------------------------------------------------


def test_contract_1_equivalence_requires_final_and_transitions() -> None:
    leaf_final = state_key(True, {})
    leaf_nonfinal = state_key(False, {})
    assert leaf_final != leaf_nonfinal  # 同为 0 个子节点，终结不同 => 不等价

    same_degree_diff_symbol = (
        state_key(False, {"a": 5}) != state_key(False, {"b": 5})
    )
    assert same_degree_diff_symbol

    same_symbol_diff_target = (
        state_key(True, {"a": 1}) != state_key(True, {"a": 2})
    )
    assert same_symbol_diff_target

    equivalent = state_key(True, {"a": 1, "b": 2}) == state_key(
        True, {"b": 2, "a": 1}
    )
    assert equivalent


def test_contract_1_builder_does_not_collapse_distinct_finals() -> None:
    # "a"（叶子终结）与需要区分的非终结结构不能被错误合并：
    # 语言 {"a", "ab"} 的最小自动机必须保留 3 个存活状态。
    dawg = build_dawg(["a", "ab"])
    assert len(dawg.states) == 3
    # 终结叶子与 a 所在终结状态不同（a 还有 b 出边）。
    a_state = None
    for sid, state in dawg.states.items():
        if state.final and state.transitions:
            a_state = sid
    assert a_state is not None
    leaf = dawg.states[a_state].transitions["b"]
    assert dawg.states[leaf].final is True
    assert dawg.states[leaf].transitions == {}


# --------------------------------------------------------------------------
# 契约 2：新增输入若不有序，明确拒绝或先排序。
# --------------------------------------------------------------------------


def test_contract_2_unordered_is_rejected_with_category() -> None:
    with pytest.raises(UnorderedCorpusError) as exc:
        CorpusSpec(sort_first=False).normalize(["z", "a"])
    assert exc.value.error_code == "unordered_corpus"


def test_contract_2_unordered_can_be_explicitly_sorted() -> None:
    result = CorpusSpec(sort_first=True).normalize(["z", "a", "m"])
    assert result.words == ["a", "m", "z"]
    assert result.was_sorted_by_us is True


def test_contract_2_sorted_with_duplicates_is_accepted() -> None:
    result = CorpusSpec().normalize(["a", "a", "b"])
    assert result.words == ["a", "b"]
    assert result.duplicate_count == 1


# --------------------------------------------------------------------------
# 契约 3：空词支持规则固定。
# --------------------------------------------------------------------------


def test_contract_3_empty_word_rule_is_fixed_and_explicit() -> None:
    # 默认拒绝。
    with pytest.raises(EmptyWordError) as exc:
        CorpusSpec().normalize([""])
    assert exc.value.error_code == "empty_word_rejected"

    # 显式允许后，根状态终结且空词被接受。
    allowed = CorpusSpec(allow_empty_word=True).normalize(["", "a"])
    dawg = build_dawg(allowed.words)
    assert dawg.states[0].final is True
    assert dawg.contains("") is True
    assert dawg.prefix_count("") == 2


# --------------------------------------------------------------------------
# 契约 4：持久化引用校验避免环和悬空状态。
# --------------------------------------------------------------------------


def test_contract_4_valid_persistence_passes(tmp_path) -> None:
    service = IndexService(SQLiteIndexRepository(tmp_path / "c4.sqlite3"))
    service.build_from_words(
        ["cat", "cats", "dog"], index_name="c4", spec=CorpusSpec()
    )
    loaded = service.load()  # 不抛异常即通过
    assert loaded.metadata.word_count == 3


def test_contract_4_dangling_reference_is_rejected() -> None:
    states = [StoredState(0, False, 1), StoredState(1, True, 1)]
    edges = [StoredEdge(0, "a", 1), StoredEdge(0, "z", 42)]
    kinds = {v.kind for v in validate_references(states, edges)}
    assert "dangling_edge" in kinds


def test_contract_4_cycle_is_rejected() -> None:
    states = [StoredState(0, False, 2), StoredState(1, True, 1), StoredState(2, True, 1)]
    edges = [
        StoredEdge(0, "a", 1),
        StoredEdge(0, "b", 2),
        StoredEdge(1, "c", 0),  # 回到根 => 环
    ]
    kinds = {v.kind for v in validate_references(states, edges)}
    assert "cycle" in kinds


def test_contract_4_corrupt_file_fails_closed(tmp_path) -> None:
    import sqlite3

    db = tmp_path / "corrupt.sqlite3"
    service = IndexService(SQLiteIndexRepository(db))
    service.build_from_words(["a", "ab"], index_name="x", spec=CorpusSpec())

    conn = sqlite3.connect(str(db))
    try:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("INSERT INTO edges (source, symbol, target) VALUES (0, 'Q', 999)")
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(IndexIntegrityError) as exc:
        service.load()
    assert any(v.kind == "dangling_edge" for v in exc.value.violations)
