"""Pydantic 协议模型：审计请求 / 响应 / 错误的线格式。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

Convention = Literal["right", "left"]
CorrelationPolicy = Literal["independent", "bounded", "explicit"]


class TransformModel(BaseModel):
    """一条坐标变换。rotation 为 3x3 矩阵或四元数 [w,x,y,z]。"""

    translation: list[float] = Field(
        ..., min_length=3, max_length=3, description="平移向量 t（长度 3）"
    )
    rotation: list[list[float]] | list[float] = Field(
        ...,
        description="旋转：3x3 旋转矩阵，或长度 4 的四元数 [w,x,y,z]",
    )


class EdgeModel(BaseModel):
    id: str = Field(..., min_length=1, description="边的唯一标识")
    parent: str = Field(..., min_length=1, description="父坐标系（变换源）")
    child: str = Field(..., min_length=1, description="子坐标系（变换目标）")
    transform: TransformModel
    covariance: list[list[float]] | None = Field(
        None,
        description=(
            "6x6 小扰动协方差，分块顺序 [平移(3), 旋转(3)]（rad）。"
            "允许省略表示该边协方差缺失；缺失会使相关传播结果标记为 "
            "unknown，绝不以独立假设静默替代。"
        ),
    )
    version: str = Field(
        ..., min_length=1, description="该边标定版本（如语义化版本或标定批次 id）"
    )


class CrossCovarianceModel(BaseModel):
    edge_a: str
    edge_b: str
    matrix: list[list[float]] = Field(..., description="6x6 互协方差 Cov(xi_a, xi_b)")


class AuditRequest(BaseModel):
    root_frame: str = Field(..., min_length=1, description="传播根坐标系")
    convention: Convention = Field(
        "right",
        description="扰动约定：right=T·Exp(ξ)（默认）或 left=Exp(ξ)·T；全链统一",
    )
    edges: list[EdgeModel] = Field(..., min_length=1)
    correlation_policy: CorrelationPolicy = Field(
        ...,
        description=(
            "边间相关性处理，必填，不得缺省为独立："
            "independent=显式声明相互独立；"
            "bounded=给定 rho_max 的等相关保守模型；"
            "explicit=逐对提供互协方差（未列出者按 unspecified_policy）"
        ),
    )
    rho_max: float | None = Field(
        None, ge=0.0, le=1.0, description="bounded 策略的等相关系数上限（含端点）"
    )
    cross_covariances: list[CrossCovarianceModel] | None = None
    unspecified_policy: Literal["independent", "bounded"] | None = Field(
        None,
        description="explicit 策略下，未显式给出互协方差的边对如何处理（必填）",
    )
    alpha: float = Field(
        0.01, gt=0.0, lt=1.0, description="闭环 χ² 检验显著性水平，默认 0.01"
    )

    @model_validator(mode="after")
    def _check_policy_fields(self) -> "AuditRequest":
        if self.correlation_policy == "bounded" and self.rho_max is None:
            raise ValueError(
                "correlation_policy=bounded 时必须提供 rho_max∈[0,1]；"
                "不允许在不给相关性上限的情况下隐式采用独立假设"
            )
        if self.correlation_policy == "explicit":
            if self.unspecified_policy is None:
                raise ValueError(
                    "correlation_policy=explicit 时必须提供 unspecified_policy，"
                    "声明未列出的边对按 independent 还是 bounded 处理"
                )
            if self.unspecified_policy == "bounded" and self.rho_max is None:
                raise ValueError(
                    "unspecified_policy=bounded 时必须提供 rho_max∈[0,1]"
                )
        if (
            self.correlation_policy != "explicit"
            and self.cross_covariances
        ):
            raise ValueError(
                "cross_covariances 仅在 correlation_policy=explicit 时提供"
            )
        return self

    @field_validator("edges")
    @classmethod
    def _unique_edge_ids(cls, v: list[EdgeModel]) -> list[EdgeModel]:
        ids = [e.id for e in v]
        if len(set(ids)) != len(ids):
            dup = sorted({i for i in ids if ids.count(i) > 1})
            raise ValueError(f"边 id 必须唯一，重复: {dup}")
        return v


class Issue(BaseModel):
    code: str
    message: str
    evidence_path: list[Any] = Field(
        default_factory=list,
        description="最短证据路径：节点/边/字段的定位序列",
    )
    details: dict[str, Any] | None = None


class PoseReport(BaseModel):
    node: str
    path_edges: list[str]
    translation: list[float]
    rotation_quaternion_wxyz: list[float]
    covariance: list[list[float]] | None
    covariance_status: Literal["ok", "unknown"]
    missing_edges: list[str] = Field(default_factory=list)


class LoopReport(BaseModel):
    nodes: list[str]
    edge_ids: list[str]
    evidence_path: list[Any]
    versions: dict[str, str]
    residual: list[float]
    residual_norm: float
    rotation_angle: float
    covariance: list[list[float]] | None
    covariance_status: Literal["ok", "unknown", "singular"]
    mahalanobis_sq: float | None
    p_value: float | None
    conflict: bool
    conflict_reason: str | None
    missing_edges: list[str] = Field(default_factory=list)


class VersionMismatch(BaseModel):
    node: str
    edge_ids: list[str]
    versions: list[str]
    message: str
    evidence_path: list[Any]


class CorrelationDeclaration(BaseModel):
    policy: str
    rho_max: float | None = None
    unspecified_policy: str | None = None
    declared_independent_pairs: list[list[str]]
    bounded_pairs: list[list[str]]
    explicit_pairs: list[list[str]]


class AuditResult(BaseModel):
    root_frame: str
    convention: str
    alpha: float
    poses: dict[str, PoseReport]
    loops: list[LoopReport]
    version_mismatches: list[VersionMismatch]
    correlation_declaration: CorrelationDeclaration
    overall: dict[str, Any]


class Envelope(BaseModel):
    """审计响应信封：结果 + 真实性签名。"""

    request_id: str
    timestamp: str
    algorithm: str = Field(
        "ed25519", description="签名算法（Ed25519，纯签名，非加密）"
    )
    key_id: str
    public_key: str
    signed_payload_sha256: str
    signature: str
    result: AuditResult


class ErrorResponse(BaseModel):
    error: dict[str, Any]
