"""Explicit energy / reachability / cost model.

All numbers are deterministic functions of documented inputs so that any
assignment can be explained to an operator (and re-checked independently).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable

from . import config

Point = tuple[float, float]


def distance(a: Point, b: Point) -> float:
    """Euclidean distance in metres."""
    return math.hypot(a[0] - b[0], a[1] - b[1])


def leg_energy(segment_m: float, payload_kg: float, loaded: bool,
               payload_factor: float = config.PAYLOAD_FACTOR_KWH_PER_M_KG,
               base_consumption: float = config.BASE_CONSUMPTION_KWH_PER_M
               ) -> float:
    """Energy for one leg: base rolling cost plus a payload surcharge."""
    effective_payload = payload_kg if loaded else 0.0
    return segment_m * (base_consumption + payload_factor * effective_payload)


def wait_energy(wait_seconds: float,
                idle_per_min: float = config.IDLE_KWH_PER_MINUTE) -> float:
    """Idle / waiting energy cost (kWh)."""
    return max(0.0, wait_seconds) / 60.0 * idle_per_min


@dataclass
class ChargerOption:
    charger_id: str
    charger_pos: Point
    return_m: float
    return_energy_kwh: float
    required_soc_kwh: float
    reserve_eta_s: float          # earliest ETA for slot contention ordering
    safety_margin_kwh: float


@dataclass
class MissionPlan:
    robot_id: str
    task_id: str
    feasible: bool
    reason: str | None = None
    outbound_m: float = 0.0
    laden_m: float = 0.0
    return_m: float = 0.0
    total_distance_m: float = 0.0
    outbound_energy_kwh: float = 0.0
    laden_energy_kwh: float = 0.0
    wait_energy_kwh: float = 0.0
    return_energy_kwh: float = 0.0
    mission_energy_kwh: float = 0.0
    safety_margin_kwh: float = 0.0
    required_soc_kwh: float = 0.0
    available_soc_kwh: float = 0.0
    energy_cost: float = 0.0
    distance_cost: float = 0.0
    wait_cost: float = 0.0
    priority_cost: float = 0.0
    total_cost: float = math.inf
    best_charger: ChargerOption | None = None
    charger_options: list[ChargerOption] = field(default_factory=list)


def robot_safety_margin(capacity_kwh: float) -> float:
    return config.SAFETY_MARGIN_FRACTION * capacity_kwh


def plan_mission(
    robot_id: str,
    robot_pos: Point,
    current_soc_kwh: float,
    capacity_kwh: float,
    task_id: str,
    pickup_pos: Point,
    delivery_pos: Point,
    payload_kg: float,
    wait_seconds: float,
    available_chargers: Iterable[tuple[str, Point]],
    priority: int = 0,
) -> MissionPlan:
    """Compute the full energy budget for one robot/task pair.

    A pair is feasible only when, after paying for outbound + laden + wait
    energy, the robot can still reach *at least one available* charger with
    the configured safety margin (fraction of capacity) left.
    """
    outbound_m = distance(robot_pos, pickup_pos)
    laden_m = distance(pickup_pos, delivery_pos)

    outbound_e = leg_energy(outbound_m, payload_kg, loaded=False)
    laden_e = leg_energy(laden_m, payload_kg, loaded=True)
    wait_e = wait_energy(wait_seconds)

    options: list[ChargerOption] = []
    margin = robot_safety_margin(capacity_kwh)

    for charger_id, charger_pos in available_chargers:
        ret_m = distance(delivery_pos, charger_pos)
        ret_e = leg_energy(ret_m, payload_kg, loaded=False)
        required = outbound_e + laden_e + wait_e + ret_e + margin
        options.append(
            ChargerOption(
                charger_id=charger_id,
                charger_pos=charger_pos,
                return_m=ret_m,
                return_energy_kwh=ret_e,
                required_soc_kwh=required,
                reserve_eta_s=outbound_m + laden_m + ret_m,
                safety_margin_kwh=margin,
            )
        )

    reachable = [o for o in options if current_soc_kwh >= o.required_soc_kwh]
    best = min(reachable, key=lambda o: o.return_m, default=None)

    mission_e = outbound_e + laden_e + wait_e + (
        best.return_energy_kwh if best else 0.0
    )
    total_d = outbound_m + laden_m + (best.return_m if best else 0.0)

    energy_cost = mission_e * config.ENERGY_PRICE_PER_KWH
    distance_cost = total_d * config.DISTANCE_PRICE_PER_M
    wait_cost = max(0.0, wait_seconds) / 60.0 * config.WAIT_PRICE_PER_MIN
    priority_cost = config.PRIORITY_BONUS * (
        config.MAX_TASK_PRIORITY - priority
    )
    total_cost = energy_cost + distance_cost + wait_cost + priority_cost

    if payload_kg < 0:
        reason = "negative_payload"
    elif not options:
        reason = "no_available_charger"
    elif not best:
        # explain *why*: the nearest charger's budget tells the clearest story
        nearest = min(options, key=lambda o: o.return_m)
        reason = (
            "unreachable"
            if current_soc_kwh < nearest.required_soc_kwh
            else "no_reachable_charger"
        )
    else:
        reason = None

    return MissionPlan(
        robot_id=robot_id,
        task_id=task_id,
        feasible=best is not None and reason is None,
        reason=reason,
        outbound_m=outbound_m,
        laden_m=laden_m,
        return_m=best.return_m if best else 0.0,
        total_distance_m=total_d,
        outbound_energy_kwh=outbound_e,
        laden_energy_kwh=laden_e,
        wait_energy_kwh=wait_e,
        return_energy_kwh=best.return_energy_kwh if best else 0.0,
        mission_energy_kwh=mission_e,
        safety_margin_kwh=margin,
        required_soc_kwh=best.required_soc_kwh if best else (
            min(o.required_soc_kwh for o in options) if options else math.inf
        ),
        available_soc_kwh=current_soc_kwh,
        energy_cost=energy_cost,
        distance_cost=distance_cost,
        wait_cost=wait_cost,
        priority_cost=priority_cost,
        total_cost=total_cost if best is not None else math.inf,
        best_charger=best,
        charger_options=sorted(options, key=lambda o: o.return_m),
    )
