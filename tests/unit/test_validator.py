"""持久化引用完整性校验器单元测试（契约 4）。

手工构造损坏记录，断言**具体的违规类别**：悬空状态、环、不可达状态、
非确定性（重复符号）、计数缺失/不一致、根缺失等。
"""

from __future__ import annotations

import pytest

from app.index.errors import (
    VIOLATION_BAD_FINAL_FLAG,
    VIOLATION_COUNT_MISMATCH,
    VIOLATION_COUNT_MISSING,
    VIOLATION_CYCLE,
    VIOLATION_DANGLING_EDGE,
    VIOLATION_DUPLICATE_STATE,
    VIOLATION_NONDETERMINISTIC,
    VIOLATION_ROOT_MISSING,
    VIOLATION_UNREACHABLE_STATE,
)
from app.index.validator import StoredEdge, StoredState, validate_references

pytestmark = pytest.mark.unit


def _kinds(violations) -> set[str]:
    return {v.kind for v in violations}


def test_valid_chain_has_no_violations() -> None:
    # 0 -a-> 1(final,count1), 根计数 1
    states = [StoredState(0, False, 1), StoredState(1, True, 1)]
    edges = [StoredEdge(0, "a", 1)]
    assert validate_references(states, edges) == []


def test_shared_target_counted_per_edge() -> None:
    # 0 -a-> 2, 0 -b-> 2；两条边各计一次 => 根计数 2。
    states = [StoredState(0, False, 2), StoredState(2, True, 1)]
    edges = [StoredEdge(0, "a", 2), StoredEdge(0, "b", 2)]
    assert validate_references(states, edges) == []


def test_root_missing() -> None:
    states = [StoredState(5, True, 1)]
    violations = validate_references(states, [])
    assert VIOLATION_ROOT_MISSING in _kinds(violations)


def test_dangling_target_detected() -> None:
    states = [StoredState(0, False, 1), StoredState(1, True, 1)]
    edges = [StoredEdge(0, "a", 1), StoredEdge(0, "b", 99)]
    violations = validate_references(states, edges)
    kinds = _kinds(violations)
    assert VIOLATION_DANGLING_EDGE in kinds
    assert any("99" in v.detail for v in violations if v.kind == VIOLATION_DANGLING_EDGE)


def test_dangling_source_detected() -> None:
    states = [StoredState(0, False, 0)]
    edges = [StoredEdge(7, "a", 0)]
    violations = validate_references(states, edges)
    assert VIOLATION_DANGLING_EDGE in _kinds(violations)


def test_unreachable_state_detected() -> None:
    states = [
        StoredState(0, False, 1),
        StoredState(1, True, 1),
        StoredState(2, True, 1),  # 无人指向
    ]
    edges = [StoredEdge(0, "a", 1)]
    assert VIOLATION_UNREACHABLE_STATE in _kinds(validate_references(states, edges))


def test_cycle_detected() -> None:
    # 0 -a-> 1, 1 -b-> 0：环。
    states = [StoredState(0, False, 1), StoredState(1, True, 1)]
    edges = [StoredEdge(0, "a", 1), StoredEdge(1, "b", 0)]
    violations = validate_references(states, edges)
    assert VIOLATION_CYCLE in _kinds(violations)


def test_self_loop_is_a_cycle() -> None:
    states = [StoredState(0, True, 1)]
    edges = [StoredEdge(0, "x", 0)]
    assert VIOLATION_CYCLE in _kinds(validate_references(states, edges))


def test_nondeterministic_duplicate_symbol_detected() -> None:
    states = [
        StoredState(0, False, 2),
        StoredState(1, True, 1),
        StoredState(2, True, 1),
    ]
    edges = [
        StoredEdge(0, "a", 1),
        StoredEdge(0, "a", 2),  # 同符号两条边
    ]
    kinds = _kinds(validate_references(states, edges))
    assert VIOLATION_NONDETERMINISTIC in kinds


def test_duplicate_state_row_detected() -> None:
    states = [
        StoredState(0, False, 1),
        StoredState(1, True, 1),
        StoredState(1, True, 1),
    ]
    assert VIOLATION_DUPLICATE_STATE in _kinds(validate_references(states, []))


def test_non_boolean_final_flag_detected() -> None:
    states = [StoredState(0, 1, 1)]  # type: ignore[arg-type]
    kinds = _kinds(validate_references(states, []))
    # 1 是 int 不是 bool；仍可能补出 root 计数问题，但必须有标记类别。
    assert VIOLATION_BAD_FINAL_FLAG in kinds


def test_word_count_missing_detected() -> None:
    states = [StoredState(0, False, None), StoredState(1, True, None)]
    edges = [StoredEdge(0, "a", 1)]
    kinds = _kinds(validate_references(states, edges))
    assert VIOLATION_COUNT_MISSING in kinds


def test_word_count_mismatch_detected() -> None:
    # 真实根计数应为 1，存成 5。
    states = [StoredState(0, False, 5), StoredState(1, True, 1)]
    edges = [StoredEdge(0, "a", 1)]
    violations = validate_references(states, edges)
    kinds = _kinds(violations)
    assert VIOLATION_COUNT_MISMATCH in kinds
    detail = next(v.detail for v in violations if v.kind == VIOLATION_COUNT_MISMATCH)
    assert "5" in detail and "1" in detail


def test_counts_skipped_when_cycle_present() -> None:
    # 有环时不做（可能不终止的）计数重算，只报环，不误报计数不一致。
    states = [StoredState(0, False, 99), StoredState(1, True, 99)]
    edges = [StoredEdge(0, "a", 1), StoredEdge(1, "b", 0)]
    kinds = _kinds(validate_references(states, edges))
    assert VIOLATION_CYCLE in kinds
    assert VIOLATION_COUNT_MISMATCH not in kinds
