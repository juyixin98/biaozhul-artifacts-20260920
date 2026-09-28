"""Unit tests for the statistical contract: validation and apportionment.

These are pure, deterministic, and assert exact failure categories rather
than merely "the API accepted input".
"""
from __future__ import annotations

import pytest

from stratblock.contract import (
    AllocationError,
    ErrorCategory,
    TailPolicy,
    build_study_config,
    canonical_stratum_key,
    hamilton_counts,
    validate_features,
)


@pytest.mark.unit
def test_hamilton_exact_and_largest_remainder_tie_break() -> None:
    assert hamilton_counts(0, (1, 1, 1)) == (0, 0, 0)
    assert hamilton_counts(4, (1, 1)) == (2, 2)
    # n=2 over three equal ratios: lowest indices win the leftover seats
    assert hamilton_counts(2, (1, 1, 1)) == (1, 1, 0)
    assert hamilton_counts(5, (2, 3)) == (2, 3)
    # 2:1 ratio, n=5 -> 3.33/1.67 -> (3, 2)
    assert hamilton_counts(5, (2, 1)) == (3, 2)
    assert sum(hamilton_counts(7, (1, 2, 3))) == 7


@pytest.mark.unit
def test_block_size_must_realise_ratio_exactly() -> None:
    with pytest.raises(AllocationError) as exc:
        build_study_config(
            "bad", ["C", "T"], ["site"], block_sizes=[2, 3],
            allocation_ratio=[1, 1],
        )
    assert exc.value.category is ErrorCategory.VALIDATION_ERROR
    assert any("block size 3" in p for p in exc.value.details["problems"])
    assert exc.value.http_status == 422


@pytest.mark.unit
def test_valid_contrasts_and_rejects() -> None:
    with pytest.raises(AllocationError) as exc:
        build_study_config("  ", ["C", "T"], ["site"], [4])
    assert exc.value.category is ErrorCategory.VALIDATION_ERROR

    with pytest.raises(AllocationError) as exc:
        build_study_config("s", ["C", "C"], ["site"], [4])
    assert any("unique" in p for p in exc.value.details["problems"])

    with pytest.raises(AllocationError) as exc:
        build_study_config("s", ["C"], ["site"], [4])
    assert any("2.." in p for p in exc.value.details["problems"])

    with pytest.raises(AllocationError) as exc:
        build_study_config(
            "s", ["C", "T"], ["site"], [4], allocation_ratio=[1, 0]
        )
    assert any("positive integers" in p for p in exc.value.details["problems"])

    with pytest.raises(AllocationError) as exc:
        build_study_config(
            "s", ["C", "T"], ["site"], [4], tail_policy="bogus"
        )
    assert any("tail_policy" in p for p in exc.value.details["problems"])

    cfg = build_study_config(
        "s", ["C", "T"], ["site"], [2, 6], [1, 1],
        TailPolicy.BALANCED_PREFIX,
    )
    assert cfg.block_sizes == (2, 6)  # sorted, frozen
    assert cfg.tail_policy is TailPolicy.BALANCED_PREFIX


@pytest.mark.unit
def test_features_must_match_frozen_factors_exactly(two_arm_cfg) -> None:
    good = validate_features(two_arm_cfg, {"site": "A"})
    assert good == {"site": "A"}

    with pytest.raises(AllocationError) as exc:
        validate_features(two_arm_cfg, {"site": "A", "extra": "x"})
    assert exc.value.details["unexpected"] == ["extra"]

    with pytest.raises(AllocationError) as exc:
        validate_features(two_arm_cfg, {})
    assert exc.value.details["missing"] == ["site"]


@pytest.mark.unit
def test_feature_levels_are_typed_strings(two_arm_cfg) -> None:
    # Integer 1 must never be silently coerced to level "1": that would
    # let feature drift redirect a subject into another stratum stream.
    with pytest.raises(AllocationError) as exc:
        validate_features(two_arm_cfg, {"site": 1})  # type: ignore[dict-item]
    assert exc.value.category is ErrorCategory.VALIDATION_ERROR

    assert canonical_stratum_key(("age", "site"), {"site": "A", "age": "old"}) == "age=old|site=A"
