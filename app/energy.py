"""显式能耗公式与可达性判定（纯函数，无副作用）。

坐标平面单位为米，电量单位为 Wh（合成模型）：

- 距离：d(a,b) = hypot(xa-xb, ya-yb)
- 行驶能耗：E_move(d, kg) = (alpha + beta * kg) * d
    alpha  空载每米能耗        BASE_RATE_WH_PER_M
    beta   每千克载荷每米附加  LOAD_RATE_WH_PER_M_KG
- 等待能耗：E_wait(s) = gamma * s
- 任务总能耗 = 满载去程 E_move(d_out, payload)
             + 现场等待 E_wait(wait_s)
             + 空载返航 E_move(d_ret, 0)   （载荷在任务点卸下）
- 可达条件：可用电量 >= 任务总能耗 + 安全余量
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from . import config


def distance(x1: float, y1: float, x2: float, y2: float) -> float:
    return math.hypot(x1 - x2, y1 - y2)


def move_energy(distance_m: float, payload_kg: float) -> float:
    return (
        config.BASE_RATE_WH_PER_M
        + config.LOAD_RATE_WH_PER_M_KG * payload_kg
    ) * distance_m


def wait_energy(wait_s: float) -> float:
    return config.WAIT_RATE_WH_PER_S * wait_s


@dataclass
class MissionEnergy:
    distance_out_m: float
    distance_return_m: float
    outbound_energy_wh: float
    wait_energy_wh: float
    return_energy_wh: float

    @property
    def total_energy_wh(self) -> float:
        return (
            self.outbound_energy_wh
            + self.wait_energy_wh
            + self.return_energy_wh
        )

    @property
    def total_distance_m(self) -> float:
        return self.distance_out_m + self.distance_return_m

    def as_dict(self) -> dict:
        return {
            "distance_out_m": round(self.distance_out_m, 6),
            "distance_return_m": round(self.distance_return_m, 6),
            "outbound_energy_wh": round(self.outbound_energy_wh, 6),
            "wait_energy_wh": round(self.wait_energy_wh, 6),
            "return_energy_wh": round(self.return_energy_wh, 6),
            "total_energy_wh": round(self.total_energy_wh, 6),
            "total_distance_m": round(self.total_distance_m, 6),
        }


def mission_energy(
    robot_x: float,
    robot_y: float,
    task_x: float,
    task_y: float,
    charger_x: float,
    charger_y: float,
    payload_kg: float,
    wait_s: float,
) -> MissionEnergy:
    d_out = distance(robot_x, robot_y, task_x, task_y)
    d_ret = distance(task_x, task_y, charger_x, charger_y)
    return MissionEnergy(
        distance_out_m=d_out,
        distance_return_m=d_ret,
        outbound_energy_wh=move_energy(d_out, payload_kg),
        wait_energy_wh=wait_energy(wait_s),
        # 返航时空载（载荷已在任务点卸下）
        return_energy_wh=move_energy(d_ret, 0.0),
    )


def residual_after_mission(
    available_battery_wh: float, mission: MissionEnergy
) -> float:
    """任务结束、抵达充电点后的剩余电量。"""
    return available_battery_wh - mission.total_energy_wh


def is_reachable(
    available_battery_wh: float, mission: MissionEnergy, safety_margin_wh: float
) -> bool:
    """完成任务后必须能抵达充电点并保留安全余量。"""
    return (
        residual_after_mission(available_battery_wh, mission)
        >= safety_margin_wh
    )
