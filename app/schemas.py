"""请求 / 响应 Pydantic 模型。"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


class RobotIn(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    x: float
    y: float
    battery_wh: float = Field(gt=0)


class ChargerIn(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    x: float
    y: float


class TaskIn(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    x: float
    y: float
    payload_kg: float = Field(ge=0)
    wait_s: float = Field(default=0.0, ge=0)


class DispatchOptions(BaseModel):
    safety_margin_wh: Optional[float] = Field(default=None, ge=0)


class DispatchRequest(BaseModel):
    task_ids: list[str] = Field(min_length=1)
    options: DispatchOptions = Field(default_factory=DispatchOptions)


class TelemetryIn(BaseModel):
    battery_wh: float = Field(gt=0)
    x: Optional[float] = None
    y: Optional[float] = None
    mission_completed_distance_m: Optional[float] = Field(
        default=None, ge=0,
        description=(
            "执行中任务自开始已实际走完的路程（去程+返航）。"
            "提供时用于计算此刻的预测剩余电量并与实测对比。"
        ),
    )


class ChargerFailureIn(BaseModel):
    reason: str = Field(default="", max_length=300)


class EnergyBreakdown(BaseModel):
    distance_out_m: float
    distance_return_m: float
    outbound_energy_wh: float
    wait_energy_wh: float
    return_energy_wh: float
    total_energy_wh: float
    total_distance_m: float


class CandidateEdge(BaseModel):
    task_id: str
    robot_id: str
    charger_id: Optional[str]
    feasible: bool
    reason: Optional[str]
    available_battery_wh: float
    residual_after_mission_wh: float
    energy: EnergyBreakdown
    selected: bool = False


class AssignmentOut(BaseModel):
    task_id: str
    robot_id: str
    charger_id: Optional[str]
    allocation_id: int
    energy: EnergyBreakdown
    reserved_energy_wh: float
    safety_margin_wh: float
    receipt: dict
    # 仅为合成的、不发送的控制指令（明确标注，不输出真实控制命令）
    simulated_command: dict


class DispatchResponse(BaseModel):
    assigned: list[AssignmentOut]
    unassigned: list[dict]
    candidate_edges: list[CandidateEdge]
    algorithm: str


class AlertOut(BaseModel):
    id: int
    kind: str
    severity: str
    robot_id: Optional[str]
    task_id: Optional[str]
    charger_id: Optional[str]
    message: str
    data_json: Optional[str]
    created_at: str
