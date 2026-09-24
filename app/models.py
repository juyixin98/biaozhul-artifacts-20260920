"""HTTP 请求/响应模型（Pydantic v2）。

签名块、信封中的字节字段均采用标准 base64（无空格），key_id 为十六进制。
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SignatureBlock(StrictModel):
    """单个签名者对同一制品的一个签名。"""

    public_key: str = Field(description="签名者 Ed25519 公钥的 PEM")
    signature: str = Field(description="Ed25519 签名的 base64")
    nonce: str = Field(description="签名者为本次签名生成的一次性随机值 base64")

    @field_validator("public_key", "signature", "nonce")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("字段不能为空")
        return v


class ArtifactEnvelope(StrictModel):
    digest: str = Field(description="制品内容的 SHA-256 十六进制摘要")
    artifact_type: str = Field(description="制品类型，如 container-image/report/plugin")
    version: str = Field(description="制品版本，如 1.4.2")
    signatures: list[SignatureBlock] = Field(min_length=1)

    @field_validator("digest")
    @classmethod
    def _digest_hex(cls, v: str) -> str:
        v = v.strip().lower()
        if len(v) != 64:
            raise ValueError("digest 必须是 64 位十六进制 SHA-256")
        int(v, 16)
        return v

    @field_validator("artifact_type", "version")
    @classmethod
    def _not_blank_text(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("字段不能为空")
        return v


class VerifyRequest(StrictModel):
    envelope: ArtifactEnvelope
    content_base64: Optional[str] = Field(
        default=None,
        description="可选：制品原文的 base64；提供后服务会重新计算摘要并与 envelope.digest 比对",
    )


class SignatureBlockResult(StrictModel):
    index: int
    key_id: Optional[str] = None
    ok: bool
    reason: Optional[str] = None


class VerifyResponse(StrictModel):
    accepted: bool
    root_version: Optional[int] = None
    reason: Optional[str] = None
    valid_signatures: int = 0
    threshold: int = 0
    blocks: list[SignatureBlockResult] = []


class RegisterRequest(VerifyRequest):
    pass


class RegisterResponse(StrictModel):
    digest: str
    artifact_type: str
    version: str
    root_version: int
    signer_key_ids: list[str]


class RootBody(StrictModel):
    """信任根描述符（不含任何签名）。"""

    version: int = Field(ge=1)
    root_threshold: int = Field(ge=1, description="轮换批准所需的旧根签名数")
    artifact_threshold: int = Field(ge=1, description="制品生效所需的制品签名数")
    root_signers: list[str] = Field(
        min_length=1, description="根角色 Ed25519 公钥 PEM（持有轮换批准权）"
    )
    artifact_signers: list[str] = Field(
        min_length=1, description="制品角色 Ed25519 公钥 PEM（持有制品签名权）"
    )


class RotateRequest(StrictModel):
    new_root: RootBody
    approvals: list[SignatureBlock] = Field(
        min_length=1, description="旧根角色密钥对新根描述符的批准签名"
    )


class SignerView(StrictModel):
    key_id: str
    public_key_pem: str


class RootView(StrictModel):
    version: int
    root_threshold: int
    artifact_threshold: int
    root_signers: list[SignerView]
    artifact_signers: list[SignerView]
