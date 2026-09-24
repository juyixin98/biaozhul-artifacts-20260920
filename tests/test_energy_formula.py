"""Explicit energy-formula checks with hand-computed numbers."""
from __future__ import annotations

import math

import pytest

from app import config
from app.energy import distance, leg_energy, plan_mission, wait_energy


def test_distance():
    assert distance((0, 0), (3, 4)) == 5.0
    assert distance((10, 10), (10, 10)) == 0.0


def test_leg_energy_empty_and_laden():
    # 100 m empty: 100 * 0.020 = 2.0 kWh
    assert leg_energy(100, 0.0, loaded=False) == pytest.approx(2.0)
    # 100 m with 50 kg: 100 * (0.020 + 0.0008*50) = 100 * 0.06 = 6.0 kWh
    assert leg_energy(100, 50.0, loaded=True) == pytest.approx(6.0)
    # payload must not affect empty legs
    assert leg_energy(100, 50.0, loaded=False) == pytest.approx(2.0)


def test_wait_energy():
    # 600 s = 10 min -> 10 * 0.05 = 0.5 kWh
    assert wait_energy(600) == pytest.approx(0.5)
    assert wait_energy(0) == 0.0


def test_plan_hand_computed():
    plan = plan_mission(
        robot_id="R", robot_pos=(0, 0), current_soc_kwh=100, capacity_kwh=100,
        task_id="T", pickup_pos=(100, 0), delivery_pos=(200, 0),
        payload_kg=50, wait_seconds=600,
        available_chargers=[("C", (200, 0))], priority=0,
    )
    assert plan.feasible
    assert plan.outbound_m == 100
    assert plan.laden_m == 100
    assert plan.return_m == 0
    assert plan.outbound_energy_kwh == pytest.approx(2.0)
    assert plan.laden_energy_kwh == pytest.approx(6.0)
    assert plan.wait_energy_kwh == pytest.approx(0.5)
    assert plan.return_energy_kwh == 0.0
    # margin = 10% * 100 = 10
    assert plan.safety_margin_kwh == pytest.approx(10.0)
    assert plan.required_soc_kwh == pytest.approx(2.0 + 6.0 + 0.5 + 0 + 10.0)
    assert plan.mission_energy_kwh == pytest.approx(8.5)
    # cost: 8.5 energy + 200*0.02 distance + 10*0.6 wait + 5*10 priority
    assert plan.energy_cost == pytest.approx(8.5)
    assert plan.distance_cost == pytest.approx(4.0)
    assert plan.wait_cost == pytest.approx(6.0)
    assert plan.priority_cost == pytest.approx(50.0)
    assert plan.total_cost == pytest.approx(68.5)
    assert plan.best_charger.charger_id == "C"


def test_feasibility_requires_margin_and_charger():
    # required = 18.5 in the plan above; 18.0 is *not* enough (margin headroom)
    plan = plan_mission(
        robot_id="R", robot_pos=(0, 0), current_soc_kwh=18.0,
        capacity_kwh=100, task_id="T", pickup_pos=(100, 0),
        delivery_pos=(200, 0), payload_kg=50, wait_seconds=600,
        available_chargers=[("C", (200, 0))])
    assert not plan.feasible
    assert plan.reason == "unreachable"

    # no chargers at all -> explicit reason
    plan2 = plan_mission(
        robot_id="R", robot_pos=(0, 0), current_soc_kwh=100,
        capacity_kwh=100, task_id="T", pickup_pos=(100, 0),
        delivery_pos=(200, 0), payload_kg=50, wait_seconds=600,
        available_chargers=[])
    assert not plan2.feasible
    assert plan2.reason == "no_available_charger"


def test_nearest_reachable_charger_chosen():
    # robot finishes at (100,0); C-far away unreachable, C-near reachable
    plan = plan_mission(
        robot_id="R", robot_pos=(0, 0), current_soc_kwh=30,
        capacity_kwh=40, task_id="T", pickup_pos=(0, 0),
        delivery_pos=(100, 0), payload_kg=0, wait_seconds=0,
        available_chargers=[("C-far", (500, 0)), ("C-near", (110, 0))])
    assert plan.feasible
    assert plan.best_charger.charger_id == "C-near"
