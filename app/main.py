"""FastAPI 应用：JWT 验证网关的 HTTP 层。

接口：
- ``GET  /health``                  健康检查
- ``GET  /issuers``                 列出受信发行方（不含密钥）
- ``POST /verify``                  提交 JWT 做验证
- ``GET  /admin/issuers``           各发行方 JWKS 缓存状态
- ``POST /admin/issuers/{id}/refresh``  强制刷新某发行方缓存

日志安全：任何拒绝都只记录**错误码 + 令牌短指纹**（SHA-256 前 12 位），
绝不记录令牌原文、签名段或其长前缀。
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .config import TrustStore, load_trust_store
from .errors import ErrCode, VerifyError
from .jwks import JwksCacheRegistry
from .verifier import JwtVerifier

logger = logging.getLogger("jwt_gateway")

TOKEN_FINGERPRINT_HEX = 12


def configure_logging() -> None:
    """配置网关自身日志（仅生产入口 ``create_app_from_env`` 调用）。

    uvicorn 的默认 dictConfig 只配置 uvicorn.* logger，若不处理，
    ``jwt_gateway`` 的 INFO 成功日志会被 root 的 lastResort（WARNING 级）
    吞掉，只剩拒绝日志。这里挂一个输出到 stderr 的 handler；级别可用
    环境变量 ``JWT_GATEWAY_LOG_LEVEL`` 覆盖。已存在 handler（外部框架
    接管日志）时不重复添加。测试路径走 ``create_app`` 不经此函数，
    保持默认 propagate，pytest caplog 正常工作。
    """
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s %(name)s: %(message)s"
            )
        )
        logger.addHandler(handler)
    level_name = os.environ.get("JWT_GATEWAY_LOG_LEVEL", "INFO").upper()
    logger.setLevel(getattr(logging, level_name, logging.INFO))


def token_fingerprint(token: str) -> str:
    """不可逆短指纹，用于把日志行与一次请求对应起来。

    取完整令牌 SHA-256 的前 12 个十六进制字符（48 bit），
    无法据此还原令牌内容。
    """
    digest = hashlib.sha256(token.encode("utf-8", errors="replace")).hexdigest()
    return f"sha256:{digest[:TOKEN_FINGERPRINT_HEX]}"


class VerifyRequest(BaseModel):
    token: str = Field(..., description="紧凑序列化的 JWT（JWS compact）")
    expected_aud: str | None = Field(
        default=None,
        description=(
            "可选：调用方在本服务期望受众之外再要求命中某个 aud。"
            "留空则只按发行方配置的 audiences 校验。"
        ),
    )


def create_app(
    trust_store: TrustStore,
    *,
    registry: JwksCacheRegistry | None = None,
    clock=time.time,
) -> FastAPI:
    registry = registry or JwksCacheRegistry(clock=clock)
    verifier = JwtVerifier(trust_store, registry, clock=clock)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> Any:
        logger.info(
            "gateway starting with %d issuer(s): %s",
            len(trust_store.issuers),
            [c.id for c in trust_store.issuers.values()],
        )
        yield

    app = FastAPI(
        title="JWT 多发行方验证网关",
        version="1.0.0",
        lifespan=lifespan,
    )

    @app.exception_handler(VerifyError)
    async def verify_error_handler(request: Request, exc: VerifyError) -> JSONResponse:
        status = 401
        if exc.code in {
            ErrCode.MALFORMED_TOKEN,
            ErrCode.MALFORMED_HEADER,
            ErrCode.MALFORMED_PAYLOAD,
            ErrCode.BAD_BASE64URL,
            ErrCode.BAD_JSON,
            ErrCode.DUPLICATE_JSON_KEY,
            ErrCode.BAD_HEADER_FIELD,
            ErrCode.BAD_CLAIM_TYPE,
            ErrCode.MISSING_HEADER_FIELD,
            ErrCode.MISSING_PAYLOAD_CLAIM,
            ErrCode.UNSUPPORTED_COMPRESSION,
            ErrCode.HEADER_PARAMETER_FORBIDDEN,
            ErrCode.UNRECOGNIZED_CRIT,
        }:
            status = 400
        body = exc.to_dict()
        body["denied"] = True
        # 日志只含错误码与短指纹；context 中可能包含 aud 等非令牌敏感数据，
        # 但绝不含令牌本身（各抛错点均已保证）。
        # token 仅用于在异常日志中计算短指纹（由 verify 依赖注入 request）。
        token = getattr(request.state, "token", "") or ""
        fp = token_fingerprint(token) if token else "-"
        logger.warning(
            "verify denied code=%s fingerprint=%s context=%s",
            exc.code,
            fp,
            exc.context,
        )
        return JSONResponse(status_code=status, content=body)

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "issuers_configured": len(trust_store.issuers)}

    @app.get("/issuers")
    async def list_issuers() -> dict[str, Any]:
        return {"issuers": trust_store.describe()}

    @app.post("/verify")
    async def verify(request: Request, req: VerifyRequest) -> dict[str, Any]:
        # 仅暂存于 request.state，供异常处理器算短指纹；不直接打日志。
        request.state.token = req.token
        result = verifier.verify(req.token)

        claims = result["claims"]
        # 调用方额外指定的 aud 必须也命中。
        if req.expected_aud is not None:
            aud = claims.get("aud")
            auds = [aud] if isinstance(aud, str) else (aud or [])
            if req.expected_aud not in auds:
                err = VerifyError(
                    ErrCode.AUD_NOT_ALLOWED,
                    "令牌 aud 未命中本次请求显式要求的 expected_aud",
                    context={
                        "expected_aud": req.expected_aud,
                        "token_aud": auds,
                    },
                )
                raise err

        logger.info(
            "verify accepted issuer_id=%s kid=%s alg=%s fingerprint=%s",
            result["issuer_id"],
            result["kid"],
            result["alg"],
            token_fingerprint(req.token),
        )
        return {
            "valid": True,
            "issuer_id": result["issuer_id"],
            "alg": result["alg"],
            "kid": result["kid"],
            "claims": claims,
        }

    @app.get("/admin/issuers")
    async def admin_issuers() -> dict[str, Any]:
        return {"caches": registry.all_snapshots()}

    @app.post("/admin/issuers/{issuer_id}/refresh")
    async def admin_refresh(issuer_id: str) -> dict[str, Any]:
        cfg = trust_store.issuers.get(issuer_id)
        if cfg is None:
            raise VerifyError(
                ErrCode.UNKNOWN_ISSUER,
                f"未知发行方 id: {issuer_id!r}",
                context={"issuer_id": issuer_id},
            )
        if cfg.jwks_uri is None:
            raise VerifyError(
                ErrCode.JWKS_MALFORMED,
                f"发行方 {issuer_id!r} 使用对称密钥，没有可刷新的 JWKS 端点",
                context={"issuer_id": issuer_id},
            )
        count = registry.for_issuer(cfg).force_refresh()
        logger.info("admin refresh issuer_id=%s keys=%d", issuer_id, count)
        return {"refreshed": True, "issuer_id": issuer_id, "keys": count}

    return app


def create_app_from_env() -> FastAPI:
    configure_logging()
    config_path = os.environ.get("JWT_GATEWAY_CONFIG", "config/issuers.json")
    store = load_trust_store(config_path)
    return create_app(store)


app = create_app_from_env()
