"""Request/response models for the calibration-chain audit service."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

Convention = Literal["right", "left"]
CorrelationPolicy = Literal["independent", "conservative_rho", "cross_blocks"]
RotationKind = Literal["quat_wxyz", "matrix"]


class RotationSpec(BaseModel):
    kind: RotationKind = "quat_wxyz"
    quat_wxyz: list[float] | None = Field(
        default=None, description="Unit quaternion [w, x, y, z] (Hamilton)."
    )
    matrix: list[list[float]] | None = Field(
        default=None, description="3x3 rotation matrix."
    )


class TransformSpec(BaseModel):
    parent_frame: str = Field(..., description="Frame the transform maps points INTO.")
    child_frame: str = Field(..., description="Frame the transform maps points FROM.")
    translation: list[float] = Field(..., min_length=3, max_length=3)
    rotation: RotationSpec
    # 6x6 small-perturbation covariance, tangent order [rho(3); phi(3)].
    # null/absent means the covariance is UNKNOWN — never silently treated as 0.
    covariance: list[list[float]] | None = None
    # How ``covariance`` is attached to the transform (defaults to request level).
    convention: Convention | None = None
    edge_id: str | None = Field(default=None, description="Optional stable edge id.")
    metadata: dict[str, str] = Field(default_factory=dict)


class CrossBlockSpec(BaseModel):
    i: str = Field(..., description="First edge id (i < j ordering is enforced).")
    j: str
    block: list[list[float]] = Field(..., description="6x6 cross-covariance block.")


class LoopSpec(BaseModel):
    name: str
    frame_ids: list[str] = Field(
        ...,
        min_length=3,
        description=(
            "Ordered closed frame walk, e.g. [A, B, C, A]. Edges are followed "
            "in the direction that matches each step; either orientation works."
        ),
    )


class AuditRequest(BaseModel):
    request_id: str | None = None
    calibration_version: str = Field(
        ..., min_length=1, description="Calibration release/version tag, e.g. semver+git sha."
    )
    convention: Convention = Field(
        "right",
        description="Perturbation convention applied to ALL edges: T = Tbar*Exp(xi) (right) or Exp(xi)*Tbar (left).",
    )
    correlation_policy: CorrelationPolicy = Field(
        ...,
        description=(
            "How unknown inter-edge correlations are handled. There is no "
            "default independence: the policy MUST be stated explicitly."
        ),
    )
    rho: float | list[list[float]] | None = Field(
        default=None,
        description="conservative_rho: scalar bound or per-edge-pair matrix in [0,1].",
    )
    cross_blocks: list[CrossBlockSpec] = Field(default_factory=list)
    edges: list[TransformSpec] = Field(..., min_length=1)
    loops: list[LoopSpec] | None = Field(
        default=None,
        description="Explicit loops; when null/empty, fundamental cycles are auto-enumerated.",
    )
    chi2_threshold: float = Field(
        default=12.592,
        ge=0.0,
        description="Mahalanobis^2 alarm threshold; default is chi2_0.95 for 6 dof.",
    )
    auto_loop_max_edges: int = Field(
        default=12, ge=3, le=64, description="Cap on enumerated loop length."
    )
    auto_loop_max_count: int = Field(default=64, ge=1, le=4096)
    # Present for signed submissions; audit itself never trusts the signature,
    # /verify does.
    signature: str | None = Field(default=None, description="hex Ed25519 signature over canonical payload.")
    public_key: str | None = Field(default=None, description="hex verify key (optional).")

    @field_validator("cross_blocks")
    @classmethod
    def _no_dup_blocks(cls, v: list[CrossBlockSpec]) -> list[CrossBlockSpec]:
        seen: set[tuple[str, str]] = set()
        for b in v:
            key = tuple(sorted((b.i, b.j)))
            if key in seen:
                raise ValueError(f"duplicate cross block for edge pair {key}")
            seen.add(key)
        return v


class ChainRequest(BaseModel):
    request_id: str | None = None
    calibration_version: str
    convention: Convention = "right"
    correlation_policy: CorrelationPolicy
    rho: float | list[list[float]] | None = None
    cross_blocks: list[CrossBlockSpec] = Field(default_factory=list)
    edges: list[TransformSpec] = Field(..., min_length=1)
    frame_path: list[str] = Field(
        ...,
        min_length=2,
        description="Ordered frame walk [F0, F1, ..., Fk] (open chain).",
    )


class SignedBundle(BaseModel):
    """Calibration payload submitted together with an Ed25519 signature."""

    payload: dict
    signature: str = Field(..., description="hex Ed25519 signature")
    public_key: str = Field(..., description="hex 32-byte Ed25519 verify key")
