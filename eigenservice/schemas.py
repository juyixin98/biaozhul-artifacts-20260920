"""FastAPI 接口模型。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class EigenRequest(BaseModel):
    matrix: list[list[float]] = Field(
        ..., description="实对称方阵, 行优先嵌套数组。"
    )
    request_id: str | None = Field(
        default=None, description="可选调用方请求身份; 缺省由服务生成。"
    )
    # 允许逐项覆盖配置 (规模与迭代预算可配置)
    max_size: int | None = None
    sym_tol: float | None = None
    residual_tol: float | None = None
    orthogonality_tol: float | None = None
    reconstruction_tol: float | None = None
    gap_tol: float | None = None
    base_sweeps: int | None = None
    sweep_multiplier: int | None = None


class EigenResponse(BaseModel):
    success: bool
    request_id: str
    service_version: str
    core: str
    size: int
    eigenvalues: list[float]
    eigenvectors: list[list[float]]
    sweeps: int
    quality: dict[str, Any]
    trace: dict[str, Any]


class ErrorResponse(BaseModel):
    success: bool = False
    request_id: str
    service_version: str
    core: str
    error_code: str
    error_message: str
    details: dict[str, Any]
    trace: dict[str, Any]
