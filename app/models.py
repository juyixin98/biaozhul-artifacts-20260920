"""API 请求/响应模型（pydantic v2）。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from app.kernels import Method


class GeneratorSpec(BaseModel):
    """合成输入声明：大流式输入不必把数组传上网。"""

    kind: Literal["cancellation", "small_accumulation", "random_spread"]
    n_pairs: int | None = Field(default=None, ge=1)
    magnitude: float = 1e16
    small: float = 1.0
    count: int | None = Field(default=None, ge=1)
    value: float = 0.1
    n: int | None = Field(default=None, ge=1)
    seed: int = 0
    lo_exp: float = -3.0
    hi_exp: float = 12.0


class SumRequest(BaseModel):
    values: list[float] | None = None
    generator: GeneratorSpec | None = None
    method: Method = Method.COMPENSATED
    chunk_size: int = Field(default=1024, ge=1)
    with_error_report: bool = True

    @model_validator(mode="after")
    def _exactly_one_source(self) -> "SumRequest":
        if (self.values is None) == (self.generator is None):
            raise ValueError("values 与 generator 必须且只能提供一个")
        return self


class CompareRequest(BaseModel):
    values: list[float] | None = None
    generator: GeneratorSpec | None = None
    chunk_size: int = Field(default=1024, ge=1)
    reorder_trials: int = Field(default=0, ge=0, le=64)
    reorder_seed: int = 0

    @model_validator(mode="after")
    def _exactly_one_source(self) -> "CompareRequest":
        if (self.values is None) == (self.generator is None):
            raise ValueError("values 与 generator 必须且只能提供一个")
        return self


class ErrorBody(BaseModel):
    category: str
    message: str
    detail: dict
    request_id: str
