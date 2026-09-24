"""FastAPI HTTP 接口（纯后端，无界面）。

端点
----
* ``GET  /healthz``              健康检查
* ``GET  /v1/trust``             查看当前信任的策略签名公钥
* ``POST /v1/evaluate``          直接提交策略集求值（开发/内网用）
* ``POST /v1/evaluate-signed``   提交 Ed25519 签名策略包求值（默认推荐）

请求体上限 1 MiB（可由环境变量 ``POLICY_MAX_BODY_BYTES`` 覆盖）。
受信公钥路径取环境变量 ``POLICY_TRUSTED_PUBLIC_KEY``，
默认 ``keys/dev_public.pem``（仅示例用途，生产环境请换密钥）。
"""
from __future__ import annotations

import hashlib
import os
from typing import Any, Dict

from cryptography.hazmat.primitives import serialization
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .crypto import load_public_key, verify_bundle
from .engine import PolicySet, evaluate_policy_set
from .errors import (
    MAX_REQUEST_BODY_BYTES,
    BundleError,
    PolicyError,
    SignatureError,
)

_TRUSTED_PUBLIC_KEY_PATH = os.environ.get(
    "POLICY_TRUSTED_PUBLIC_KEY", "keys/dev_public.pem"
)
_MAX_BODY = int(os.environ.get("POLICY_MAX_BODY_BYTES", str(MAX_REQUEST_BODY_BYTES)))


class BodyLimitASGI:
    """纯 ASGI 中间件：包裹 receive 分块计数；超限则短路返回 413。

    两条路径：
    * 有 Content-Length 且超限：读取前直接拒绝（不消费 body）；
    * 无 Content-Length（chunked）：计数分块，超限后把后续 receive
      短路为空 ``http.request``，使解析得到空体并走 4xx 校验，
      但我们已先发送唯一的 413 响应，下游响应被忽略。
    """

    def __init__(self, app, max_bytes_provider):
        self.app = app
        # 以函数形式提供，便于测试动态调整与部署时读环境变量
        self.max_bytes_provider = max_bytes_provider

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        max_bytes = self.max_bytes_provider()

        async def reject():
            payload = (
                b'{"error":{"code":"payload_too_large",'
                b'"message":"request body exceeds the configured limit"}}'
            )
            await send(
                {"type": "http.response.start", "status": 413,
                 "headers": [(b"content-type", b"application/json"),
                             (b"content-length", str(len(payload)).encode())]}
            )
            await send({"type": "http.response.body", "body": payload})

        # 快速路径：Content-Length 已表明超限
        cl = next((v for k, v in scope.get("headers", []) if k == b"content-length"), None)
        if cl is not None:
            try:
                if int(cl) > max_bytes:
                    await reject()
                    return
            except ValueError:
                await self.app(scope, receive, send)
                return

        received = 0
        rejected = False

        async def limited_receive():
            nonlocal received, rejected
            if rejected:
                # 下游继续读取时返回空体并声明结束，避免重复读客户端
                return {"type": "http.request", "body": b"", "more_body": False}
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > max_bytes:
                    rejected = True
                    await reject()
            return message

        await self.app(scope, limited_receive, send)


app = FastAPI(
    title="Offline Policy Evaluation Interpreter",
    version="1.0.0",
    description="声明式资源访问策略解释器：主体/资源属性、集合包含、"
    "逻辑组合、显式拒绝优先；缺失属性按未知（拒绝）处理；禁止执行任意代码。",
)
app.add_middleware(BodyLimitASGI, max_bytes_provider=lambda: _MAX_BODY)


class EvaluateRequest(BaseModel):
    policies: list = Field(description="策略对象数组")
    subject: Dict[str, Any] = Field(default_factory=dict)
    resource: Dict[str, Any] = Field(default_factory=dict)


class SignedEvaluateRequest(BaseModel):
    bundle: Dict[str, Any] = Field(description="Ed25519 签名策略包")
    subject: Dict[str, Any] = Field(default_factory=dict)
    resource: Dict[str, Any] = Field(default_factory=dict)


def _error(status: int, code: str, message: str, details: list[str] | None = None) -> JSONResponse:
    body: Dict[str, Any] = {"error": {"code": code, "message": message}}
    if details:
        body["error"]["details"] = details
    return JSONResponse(status_code=status, content=body)


@app.exception_handler(PolicyError)
async def policy_error_handler(_request: Request, exc: PolicyError) -> JSONResponse:
    return _error(400, "invalid_policy", "策略结构或语义非法", exc.details)


@app.exception_handler(BundleError)
async def bundle_error_handler(_request: Request, exc: BundleError) -> JSONResponse:
    return _error(400, "invalid_bundle", str(exc))


@app.exception_handler(SignatureError)
async def signature_error_handler(_request: Request, exc: SignatureError) -> JSONResponse:
    return _error(403, "invalid_signature", str(exc))


@app.get("/healthz")
async def healthz() -> Dict[str, str]:
    return {"status": "ok"}


@app.get("/v1/trust")
async def trust_info() -> Dict[str, Any]:
    key = load_public_key(_TRUSTED_PUBLIC_KEY_PATH)
    if key is None:
        return {
            "configured": False,
            "path": _TRUSTED_PUBLIC_KEY_PATH,
            "message": "未配置受信公钥，/v1/evaluate-signed 不可用",
        }
    der = key.public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return {
        "configured": True,
        "path": _TRUSTED_PUBLIC_KEY_PATH,
        "alg": "Ed25519",
        "sha256": hashlib.sha256(der).hexdigest(),
    }


@app.post("/v1/evaluate")
async def evaluate(req: EvaluateRequest) -> Dict[str, Any]:
    """对内存中直接给出的策略集求值（不经签名校验）。"""
    policy_set = PolicySet(req.policies)
    policy_set.validate()
    return evaluate_policy_set(policy_set, req.subject, req.resource)


@app.post("/v1/evaluate-signed")
async def evaluate_signed(req: SignedEvaluateRequest) -> Dict[str, Any]:
    """对签名策略包先验签再求值；公钥不被信任则拒绝（403）。"""
    public_key = load_public_key(_TRUSTED_PUBLIC_KEY_PATH)
    if public_key is None:
        return _error(
            503,
            "trust_not_configured",
            f"服务器未配置受信公钥（查找路径 {_TRUSTED_PUBLIC_KEY_PATH}），"
            "无法验证签名策略包",
        )
    policies = verify_bundle(req.bundle, public_key)
    policy_set = PolicySet(policies)
    policy_set.validate()
    return evaluate_policy_set(policy_set, req.subject, req.resource)
