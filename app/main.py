"""FastAPI 应用：本地证书链验证 HTTP 接口。"""

from __future__ import annotations

from datetime import datetime

from fastapi import FastAPI
from pydantic import BaseModel, Field

from app.verify import (
    CertificateInputError,
    VerificationResult,
    verify_certificate_chain,
)

app = FastAPI(
    title="本地证书链验证服务",
    description="离线 X.509 证书链验证：核对时间、用途、路径长度与 DNS 名，不访问在线吊销服务。",
    version="1.0.0",
)


class VerifyRequest(BaseModel):
    leaf_certificate_pem: str = Field(..., description="叶证书 PEM")
    intermediate_certificates_pem: list[str] = Field(
        default_factory=list, description="候选中间证书 PEM 列表（顺序不限）"
    )
    trust_roots_pem: list[str] = Field(..., description="显式信任根 PEM 列表")
    expected_dns_name: str = Field(..., description="期望的 DNS 名")
    validation_time: datetime | None = Field(
        default=None, description="验证时刻（ISO 8601），缺省为当前 UTC 时间"
    )


class VerifyResponse(BaseModel):
    valid: bool
    chain_subjects: list[str] = []
    error: str | None = None


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/api/v1/verify", response_model=VerifyResponse)
def verify(req: VerifyRequest) -> VerifyResponse:
    try:
        result: VerificationResult = verify_certificate_chain(
            leaf_pem=req.leaf_certificate_pem,
            intermediates_pem=req.intermediate_certificates_pem,
            trust_roots_pem=req.trust_roots_pem,
            expected_dns_name=req.expected_dns_name,
            validation_time=req.validation_time,
        )
    except CertificateInputError as exc:
        # 输入无法解析：返回 200 + valid=false，错误信息说明是输入问题
        return VerifyResponse(valid=False, error=f"输入错误: {exc}")
    return VerifyResponse(
        valid=result.valid,
        chain_subjects=result.chain_subjects,
        error=result.error,
    )
