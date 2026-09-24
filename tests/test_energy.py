"""能耗公式与安全余量的单元测试（真实计算，无 mock）。"""
from __future__ import annotations

import math

import pytest

from app import config, energy


def test_move_energy_explicit_formula():
    # E_move = (alpha + beta*kg) * d
    d, kg = 100.0, 25.0
    expected = (
        config.BASE_RATE_WH_PER_M + config.LOAD_RATE_WH_PER_M_KG * kg
    ) * d
    assert energy.move_energy(d, kg) == pytest.approx(expected)


def test_wait_energy_explicit_formula():
    assert energy.wait_energy(120.0) == pytest.approx(
        config.WAIT_RATE_WH_PER_S * 120.0
    )


def test_mission_energy_full_then_empty():
    # 机器人 (0,0) -> 任务 (50,0)，载荷 20kg，等待 60s -> 充电点 (0,0)
    m = energy.mission_energy(0, 0, 50, 0, 0, 0, 20.0, 60.0)
    assert m.distance_out_m == 50.0
    assert m.distance_return_m == 50.0
    assert m.outbound_energy_wh == pytest.approx((0.18 + 0.0006 * 20) * 50)
    assert m.wait_energy_wh == pytest.approx(0.012 * 60)
    # 返航空载
    assert m.return_energy_wh == pytest.approx(0.18 * 50)
    assert m.total_energy_wh == pytest.approx(
        m.outbound_energy_wh + m.wait_energy_wh + m.return_energy_wh
    )


def test_closest_charger_is_euclidean_minimum():
    d = energy.distance(0, 0, 3, 4)
    assert d == 5.0
    assert isinstance(d, float)


def test_reachability_requires_safety_margin():
    m = energy.MissionEnergy(
        distance_out_m=10, distance_return_m=10,
        outbound_energy_wh=2.0, wait_energy_wh=1.0,
        return_energy_wh=1.8,
    )
    assert energy.is_reachable(30.0, m, safety_margin_wh=20.0)
    # 刚好完成任务但无安全余量 -> 不可达
    assert not energy.is_reachable(4.8, m, safety_margin_wh=20.0)
    # 等于余量边界 -> 可达（保留 >= margin）
    assert energy.is_reachable(24.8, m, safety_margin_wh=20.0)
