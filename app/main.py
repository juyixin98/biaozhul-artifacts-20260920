"""FastAPI 应用：标定链传播审计服务（纯后端 JSON API）。

端点
----
* ``GET  /health``             存活与密钥指纹
* ``POST /audit``              执行标定链审计，返回签名信封
* ``POST /verify``             验证审计信封签名
* ``POST /montecarlo/selfcheck``  用合成样本核对一阶传播，返回拟合报告
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from . import __version__
from .crypto import Signer, verify_signature
from .engine import AuditError, run_audit
from .models import AuditRequest
from .scenarios import build_selfcheck

_signer: Signer | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _signer
    _signer = Signer.load_or_create()
    yield


app = FastAPI(
    title="标定链传播审计",
    version=__version__,
    lifespan=lifespan,
    description=(
        "传感器外参链协方差一阶传播、闭环 χ² 检验与标定版本审计。"
        "统一左/右扰动约定；未知相关性必须显式声明，不静默假设独立。"
    ),
)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


@app.exception_handler(AuditError)
async def _audit_error_handler(request: Request, exc: AuditError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "type": "validation_failed",
                "message": "输入未通过标定合法性/正定性校验",
                "issues": exc.issues,
            }
        },
    )


@app.exception_handler(ValidationError)
async def _pydantic_error_handler(
    request: Request, exc: ValidationError
) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "type": "schema_validation_failed",
                "message": "请求不符合协议模式",
                "issues": [
                    {
                        "code": "SCHEMA",
                        "message": e.get("msg", ""),
                        "evidence_path": [str(x) for x in e.get("loc", [])],
                    }
                    for e in exc.errors()
                ],
            }
        },
    )


@app.get("/health")
async def health() -> dict:
    assert _signer is not None
    return {
        "status": "ok",
        "service": "calibration-chain-audit",
        "version": __version__,
        "key_id": _signer.key_id,
        "time": _utcnow(),
    }


@app.post("/audit")
async def audit(req: AuditRequest) -> dict:
    assert _signer is not None
    result = run_audit(req)
    digest, signature, pubkey = _signer.sign_result(result)
    request_id = str(uuid.uuid4())
    return {
        "request_id": request_id,
        "timestamp": _utcnow(),
        "algorithm": "ed25519",
        "key_id": _signer.key_id,
        "public_key": pubkey,
        "signed_payload_sha256": digest,
        "signature": signature,
        "result": result,
    }


@app.post("/verify")
async def verify(request: Request) -> JSONResponse:
    body = await request.json()
    try:
        pub = body["public_key"]
        sig = body["signature"]
        result = body.get("result", body.get("payload"))
    except (KeyError, TypeError):
        return JSONResponse(
            status_code=400,
            content={
                "valid": False,
                "message": "需要 public_key、signature、result/payload 字段",
            },
        )
    if result is None:
        return JSONResponse(
            status_code=400,
            content={"valid": False, "message": "缺少待验证的 result/payload"},
        )
    valid = verify_signature(pub, result, sig)
    return JSONResponse(
        status_code=200 if valid else 400,
        content={
            "valid": valid,
            "message": "Ed25519 签名验证通过" if valid else "签名无效或载荷被篡改",
        },
    )


@app.post("/montecarlo/selfcheck")
async def montecarlo_selfcheck(
    n_samples: int | None = None, seed: int | None = None
) -> dict:
    """运行 Monte Carlo 自检（小角度 / 长链 / 缺失协方差 / 相关性）。"""
    report = build_selfcheck(
        n_samples=n_samples or 30000, seed=seed if seed is not None else 0
    )
    return report
