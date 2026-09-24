"""HTTP 接口的请求 / 响应模型。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


# --- 信任根 ------------------------------------------------------------------


class RootInitRequest(BaseModel):
    root_version: int = Field(..., ge=1, description="根版本号，首个根通常为 1")
    threshold: int = Field(..., ge=1, description="轮换所需的旧根批准阈值")
    threshold_public_keys: list[str] = Field(..., min_length=1, description="阈值成员公钥(hex)")
    signer_public_keys: list[str] = Field(..., min_length=1, description="制品签名公钥(hex)")


class RootRotateRequest(BaseModel):
    new_root_version: int = Field(..., ge=1)
    new_threshold: int = Field(..., ge=1)
    new_threshold_public_keys: list[str] = Field(..., min_length=1)
    new_signer_public_keys: list[str] = Field(..., min_length=1)
    # 每项：{"key_id": ..., "signature": ...} —— 旧根阈值成员的批准
    approvals: list[dict[str, str]] = Field(..., min_length=1)


class RootView(BaseModel):
    root_version: int
    threshold: int
    threshold_keys: dict[str, str] = Field(
        description="key_id -> 公钥(hex)，即当前阈值成员"
    )
    signer_keys: dict[str, str] = Field(description="key_id -> 公钥(hex)，即授权制品签名者")


# --- 制品签名 ----------------------------------------------------------------


class ArtifactSignRequest(BaseModel):
    artifact_type: str = Field(..., min_length=1, description="制品类型，参与签名绑定")
    version: str = Field(..., description="语义版本 MAJOR.MINOR.PATCH")
    digest: str = Field(..., description="制品正文 SHA-256 摘要(hex)")
    key_id: str = Field(..., description="签名公钥的 key_id")
    nonce: str = Field(..., description="一次性随机值，16 字节 hex(32 字符)")
    signature: str = Field(..., description="Ed25519 签名(hex)")


class ArtifactRecord(BaseModel):
    artifact_type: str
    version: str
    digest: str
    key_id: str
    public_key: str
    nonce: str
    signature: str
    registered_at_root_version: int


# --- 验签 --------------------------------------------------------------------


class VerifyRequest(BaseModel):
    artifact_type: str
    version: str
    digest: str
    nonce: str
    signature: str
    # 无状态验签时由调用方提供公钥(hex)；留空则要求该制品已在服务登记
    public_key: str | None = None
    key_id: str | None = None


class VerifyResponse(BaseModel):
    valid: bool
    reason: str
    artifact_type: str
    version: str
    key_id: str | None = None
    signer_trusted: bool | None = None


# --- 通用 --------------------------------------------------------------------


class OkResponse(BaseModel):
    ok: Literal[True] = True
    detail: str
