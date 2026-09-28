"""统计契约校验与指纹稳定性。"""
from __future__ import annotations

import pytest

from app.contracts import build_contract
from app.errors import AppError, ErrorCategory


def _ok_kwargs(**overrides):
    kw = dict(
        study_id="S1",
        arm_specs=[("A", 1), ("B", 1)],
        factor_specs=[("site", ["x", "y"])],
        block_multiple=1,
        tail_policy="keep_open",
        seed_fingerprint="seed_" + "0" * 64,
        seed_proof="pbkdf2-sha256$1$AA$AA",
    )
    kw.update(overrides)
    return kw


def test_requires_two_arms():
    with pytest.raises(AppError) as ei:
        build_contract(**_ok_kwargs(arm_specs=[("A", 1)]))
    assert ei.value.category == ErrorCategory.INVALID_CONTRACT


def test_rejects_non_positive_ratio_and_duplicate_arm():
    with pytest.raises(AppError) as ei:
        build_contract(**_ok_kwargs(arm_specs=[("A", 0), ("B", 1)]))
    assert ei.value.category == ErrorCategory.INVALID_CONTRACT
    with pytest.raises(AppError) as ei:
        build_contract(**_ok_kwargs(arm_specs=[("A", 1), ("A", 2)]))
    assert ei.value.category == ErrorCategory.INVALID_CONTRACT


def test_rejects_unknown_tail_policy():
    with pytest.raises(AppError) as ei:
        build_contract(**_ok_kwargs(tail_policy="whatever"))
    assert ei.value.category == ErrorCategory.INVALID_CONTRACT


def test_block_size_is_ratio_sum_multiple():
    c = build_contract(**_ok_kwargs(
        arm_specs=[("A", 1), ("B", 2)], block_multiple=3))
    assert c.ratio_sum == 3
    assert c.block_size == 9
    assert c.arm_slots_per_block() == {"A": 3, "B": 6}


def test_feature_validation_categories():
    c = build_contract(**_ok_kwargs())
    with pytest.raises(AppError) as ei:
        c.validate_features({"site": "x", "extra": "z"})
    assert ei.value.category == ErrorCategory.UNEXPECTED_STRATUM_FACTOR
    with pytest.raises(AppError) as ei:
        c.validate_features({})
    assert ei.value.category == ErrorCategory.MISSING_STRATUM_FACTOR
    with pytest.raises(AppError) as ei:
        c.validate_features({"site": "not-a-level"})
    assert ei.value.category == ErrorCategory.NON_DISCRETE_STRATUM_VALUE
    with pytest.raises(AppError) as ei:
        c.validate_features({"site": ""})
    assert ei.value.category == ErrorCategory.EMPTY_STRATUM_VALUE


def test_fingerprint_stable_and_sensitive():
    c1 = build_contract(**_ok_kwargs())
    c2 = build_contract(**_ok_kwargs())  # 同内容
    c3 = build_contract(**_ok_kwargs(block_multiple=2))
    assert c1.fingerprint() == c2.fingerprint()
    assert c1.fingerprint() != c3.fingerprint()
    assert c1.fingerprint().startswith("ctr_")
    # 持有证明不影响指纹（proof 含随机盐，进入指纹会破坏可复现性）
    c4 = build_contract(**_ok_kwargs(seed_proof="pbkdf2-sha256$9$BB$CC"))
    assert c4.fingerprint() == c1.fingerprint()


def test_stratum_key_order_follows_contract():
    c = build_contract(**_ok_kwargs(
        factor_specs=[("a", ["1"]), ("b", ["2"])]))
    # 即使传入字典顺序不同，层键也应一致
    assert c.stratum_key({"a": "1", "b": "2"}) == "1|2"
    assert c.stratum_key({"b": "2", "a": "1"}) == "1|2"
