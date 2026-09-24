"""HTTP/JSON 协议的 Pydantic 模型。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# 全协议禁止 NaN / Infinity，避免污染 Kalman 数值
_ModelConfig = ConfigDict(extra="forbid", allow_inf_nan=False)


class DetectionIn(BaseModel):
    model_config = _ModelConfig

    x: float = Field(..., description="检测点 x 坐标")
    y: float = Field(..., description="检测点 y 坐标")
    detection_id: str | None = Field(
        default=None, description="可选检测标识，仅用于审计，不参与关联"
    )


class TrackerConfigIn(BaseModel):
    model_config = _ModelConfig

    confirm_hits: int = Field(default=3, ge=1, le=100)
    max_misses: int = Field(default=5, ge=1, le=1000)
    gate_threshold: float = Field(default=9.210, gt=0, le=1000.0)
    merge_radius: float = Field(default=0.25, ge=0.0, le=1000.0)
    process_noise: float = Field(default=1.0, gt=0.0, le=1e6)
    measurement_var: float = Field(default=1.0, gt=0.0, le=1e6)
    init_pos_var: float = Field(default=10.0, gt=0.0, le=1e8)
    init_vel_var: float = Field(default=100.0, gt=0.0, le=1e8)


class CreateSessionIn(BaseModel):
    model_config = _ModelConfig

    session_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        description="可选自定义会话 ID；省略则由服务端生成",
    )
    config: TrackerConfigIn | None = None


class FrameIn(BaseModel):
    model_config = _ModelConfig

    frame_id: int = Field(..., ge=0, description="帧序号，必须严格递增")
    timestamp: float = Field(..., ge=0.0, description="帧时间戳（秒），不得回退")
    detections: list[DetectionIn] = Field(default_factory=list)


# ----------------------------------------------------------------- responses
class AssignmentOut(BaseModel):
    track_id: int
    detection_index: int
    predicted_xy: list[float]
    measurement_xy: list[float]
    euclidean_distance: float
    mahalanobis_sq: float
    gate_threshold: float
    within_gate: bool
    selected: bool


class TrackOut(BaseModel):
    track_id: int
    state: Literal["tentative", "confirmed", "coasting", "deleted"]
    hits: int
    hit_streak: int
    misses: int
    position: list[float]
    velocity: list[float]
    position_variance: list[float]
    associated_detection_index: int | None
    age_frames: int


class FrameOut(BaseModel):
    session_id: str
    frame_id: int
    timestamp: float
    dt: float | None
    detections: list[dict]
    merged_groups: list[list[str]]
    tracks: list[TrackOut]
    assignments: list[AssignmentOut]
    rejected_by_gate: list[AssignmentOut]
    births: list[int]
    matched: list[list[int]]
    coasted: list[int]
    deleted: list[int]
    next_track_id: int
    selection_basis: dict
    replay: bool
    signature: str
    signature_algorithm: str


class SessionOut(BaseModel):
    session_id: str
    config: dict
    last_frame_id: int | None
    last_timestamp: float | None
    alive_track_count: int
    next_track_id: int
    # HMAC 密钥仅在创建时返回一次，用于客户端校验响应签名
    signing_key_hex: str | None = None
    signature_algorithm: str = "HMAC-SHA256(canonical_json(body_without_signature))"


class HealthOut(BaseModel):
    status: Literal["ok"]
    sessions: int
