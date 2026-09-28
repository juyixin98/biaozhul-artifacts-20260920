"""分布诊断与均衡证据测试。

统计结论被严格约束：测试断言"在固定种子批次上未检出偏离"，并验证
代码*没有*把检验不显著表述成"证明正确"。
"""
from __future__ import annotations

import math

import pytest

from app.evidence.distribution import (
    FIXED_SEEDS_B64, arm_frequency_by_position,
    enumerate_permutation_distribution,
)


@pytest.mark.slow
def test_permutation_uniformity_small_blocks():
    for k in (2, 3, 4):
        res = enumerate_permutation_distribution(k, blocks_per_seed=300)
        assert res["possible_permutations"] == math.factorial(k)
        assert res["n_draws"] == 300 * len(FIXED_SEEDS_B64)
        # 每种置换都应至少出现（样本量远大于 K!）
        assert all(row["count"] > 0 for row in res["permutation_table"])
        assert res["departure_detected"] is False
        assert "不等于证明" in res["conclusion"]


def test_permutation_enumeration_rejects_large_block():
    with pytest.raises(ValueError):
        enumerate_permutation_distribution(6)


@pytest.mark.slow
def test_position_arm_frequency_equal_arms():
    arms = ("A", "B")
    slot_arms = ("A", "A", "B", "B")
    res = arm_frequency_by_position(arms, slot_arms, 4, blocks_per_seed=300)
    assert res["departure_detected"] is False
    for p in res["positions"]:
        assert set(p["counts"])  # 非零
        total = sum(p["counts"].values())
        # 经验比例接近 0.5，给 6 个点的宽容差（这只是健全性，不是证明）
        for a in arms:
            assert abs(p["counts"][a] / total - 0.5) < 0.08


@pytest.mark.slow
def test_position_arm_frequency_unequal_ratio():
    arms = ("A", "B")
    # 1:2, multiple=2 → A,A,B,B,B,B
    slot_arms = ("A", "A", "B", "B", "B", "B")
    res = arm_frequency_by_position(arms, slot_arms, 6, blocks_per_seed=300)
    assert res["departure_detected"] is False
    for p in res["positions"]:
        total = sum(p["counts"].values())
        assert abs(p["counts"]["A"] / total - 1 / 3) < 0.08
        assert abs(p["counts"]["B"] / total - 2 / 3) < 0.08


def test_distribution_reports_seed_sources():
    res = enumerate_permutation_distribution(2, blocks_per_seed=50)
    assert res["seeds"] == list(FIXED_SEEDS_B64)
    assert len(res["seeds"]) >= 3  # 多种子
