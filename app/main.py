"""FastAPI 应用：HTTP 接口与错误处理。

环境变量：
- TRUST_STORE_FILE：状态落盘路径（可选；不设则纯内存，重启即重置，适合演示/测试）。
- 服务端从不持有任何私钥：签名在本地用 scripts/ 下工具完成。
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .errors import ServiceError
from .models import (
    RegisterRequest,
    RegisterResponse,
    RootBody,
    RootView,
    RotateRequest,
    VerifyRequest,
    VerifyResponse,
)
from .store import TrustStore


def create_app(persist_path: str | None = None) -> FastAPI:
    persist_path = persist_path or os.environ.get("TRUST_STORE_FILE")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.store = TrustStore(persist_path).load()
        yield

    app = FastAPI(
        title="制品签名与信任轮换服务",
        version="1.0.0",
        description=(
            "基于 Ed25519 的本地制品签名验证服务。签名绑定内容摘要、制品类型与版本；"
            "信任根轮换须由旧根阈值签名批准，并拒绝版本回退。"
        ),
        lifespan=lifespan,
    )

    @app.exception_handler(ServiceError)
    async def _service_error(request: Request, exc: ServiceError):
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    @app.get("/health")
    def health():
        root = app.state.store.root
        return {"status": "ok", "root_version": None if root is None else root.version}

    @app.get("/roots/current", response_model=RootView | None)
    def current_root():
        root = app.state.store.root
        return None if root is None else root.to_view()

    @app.post("/roots/bootstrap", response_model=RootView, status_code=201)
    def bootstrap(body: RootBody):
        return app.state.store.bootstrap(body)

    @app.post("/roots/rotate", response_model=RootView)
    def rotate(req: RotateRequest):
        return app.state.store.rotate(req.new_root, req.approvals)

    @app.post("/verify", response_model=VerifyResponse)
    def verify(req: VerifyRequest):
        return app.state.store.verify(req.envelope, req.content_base64)

    @app.post("/artifacts/register", response_model=RegisterResponse, status_code=201)
    def register(req: RegisterRequest):
        return app.state.store.register(req.envelope, req.content_base64)

    return app


app = create_app()
