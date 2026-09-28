"""请求/响应模型（Pydantic v2）。"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from ..contracts import MAX_ARMS, MAX_FACTORS, TAIL_KEEP_OPEN


class ArmSpecIn(BaseModel):
    arm_id: str = Field(min_length=1, max_length=64)
    ratio: int = Field(ge=1, le=1000)


class FactorSpecIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    levels: list[str] = Field(min_length=1, max_length=64)


class StudyCreateIn(BaseModel):
    study_id: str = Field(min_length=1, max_length=64)
    arms: list[ArmSpecIn] = Field(min_length=2, max_length=MAX_ARMS)
    factors: list[FactorSpecIn] = Field(default_factory=list,
                                        max_length=MAX_FACTORS)
    block_multiple: int = Field(ge=1, le=100)
    tail_policy: Literal["keep_open", "seal_early"] = TAIL_KEEP_OPEN
    # 必填：服务永不隐式生成种子。base64 编码的 16..1024 字节。
    seed_base64: str = Field(min_length=22)


class AllocateIn(BaseModel):
    subject_id: str = Field(min_length=1, max_length=128)
    features: dict[str, str]


class OpenBlockIn(BaseModel):
    stratum_key: str = Field(min_length=1, max_length=256)


class DistributionProbeIn(BaseModel):
    block_size: int = Field(ge=2, le=12)
    arms: list[ArmSpecIn] = Field(min_length=2, max_length=MAX_ARMS,
                                 default_factory=lambda: [
                                     ArmSpecIn(arm_id="A", ratio=1),
                                     ArmSpecIn(arm_id="B", ratio=1),
                                 ])
    blocks_per_seed: int = Field(default=200, ge=50, le=2000)
