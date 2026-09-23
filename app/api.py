"""FastAPI 应用：装配仓储、密钥与路由。

提供 create_app(...) 工厂便于测试注入；模块级 app 使用环境配置。
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from .config import Settings
from .crypto import SigningKey, VerifyKey, load_signing_key, load_verify_key
from .repository import MemoryRepository, PgRepository
from .schemas import EventBatchIn
from .service import ReplayService, ServiceError


def _load_keys(settings: Settings) -> tuple[VerifyKey | None, SigningKey]:
    kd = os.path.abspath(settings.keys_dir)
    verify_key = None
    if settings.require_signatures:
        verify_key = load_verify_key(
            settings.feeder_public_key_hex, os.path.join(kd, "feeder_public.hex")
        )
    signing_key = load_signing_key(
        settings.server_private_key_hex, os.path.join(kd, "server_private.hex")
    )
    return verify_key, signing_key


def create_app(
    settings: Settings,
    *,
    verify_key: VerifyKey | None = None,
    signing_key: SigningKey | None = None,
    repo: MemoryRepository | None = None,
) -> FastAPI:
    """组装应用。repo 可注入内存库（测试用）；否则按 settings 建 PG 引擎。"""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        vk, sk = _load_keys(settings)
        app.state.verify_key = vk if verify_key is None else verify_key
        app.state.signing_key = sk if signing_key is None else signing_key
        app.state.engine = None
        app.state.session_maker = None
        if repo is not None:
            app.state.memory_repo = repo
        elif settings.database_url:
            engine = create_async_engine(settings.database_url, pool_pre_ping=True)
            maker = async_sessionmaker(engine, expire_on_commit=False)
            async with maker() as session:
                await PgRepository(session).init_schema()
            app.state.engine = engine
            app.state.session_maker = maker
            app.state.memory_repo = None
        else:
            app.state.memory_repo = MemoryRepository()
        yield
        if app.state.engine is not None:
            await app.state.engine.dispose()

    application = FastAPI(title="借贷清算回放器", version="1.0.0", lifespan=lifespan)

    async def get_service(request: Request) -> ReplayService:
        if application.state.session_maker is not None:
            session = application.state.session_maker()
            request.state._pg_session = session
            return ReplayService(
                PgRepository(session),
                settings,
                application.state.verify_key,
                application.state.signing_key,
            )
        return ReplayService(
            application.state.memory_repo,
            settings,
            application.state.verify_key,
            application.state.signing_key,
        )

    @application.middleware("http")
    async def close_pg_session(request: Request, call_next):
        try:
            return await call_next(request)
        finally:
            sess = getattr(request.state, "_pg_session", None)
            if sess is not None:
                await sess.close()

    @application.exception_handler(ServiceError)
    async def service_error_handler(request: Request, exc: ServiceError):
        return JSONResponse(
            status_code=exc.status, content={"error": exc.code, "detail": str(exc)}
        )

    @application.get("/health")
    async def health():
        return {
            "status": "ok",
            "storage": "postgresql" if settings.database_url else "memory",
            "require_signatures": settings.require_signatures,
            "price_staleness_seconds": settings.price_staleness_seconds,
        }

    @application.post("/events")
    async def post_events(batch: EventBatchIn, svc: ReplayService = Depends(get_service)):
        raw = [ev.model_dump() for ev in batch.events]
        return await svc.ingest_batch(raw)

    @application.get("/events")
    async def get_events(svc: ReplayService = Depends(get_service)):
        return {"events": await svc.events()}

    @application.get("/reports")
    async def list_reports(svc: ReplayService = Depends(get_service)):
        return {"reports": await svc.repo.list_report_versions()}

    @application.get("/reports/latest")
    async def latest_report(svc: ReplayService = Depends(get_service)):
        report = await svc.latest_report()
        if report is None:
            return JSONResponse(status_code=404, content={"error": "no_report"})
        return report

    @application.get("/reports/{version}")
    async def get_report(version: int, svc: ReplayService = Depends(get_service)):
        report = await svc.report(version)
        if report is None:
            return JSONResponse(status_code=404, content={"error": "not_found"})
        return report

    if settings.allow_reset:

        @application.post("/admin/reset")
        async def admin_reset(svc: ReplayService = Depends(get_service)):
            await svc.repo.reset()
            return {"status": "reset"}

    return application


# 模块级应用（uvicorn app.api:app），配置来自环境变量
from .config import settings as default_settings  # noqa: E402

app = create_app(default_settings)
