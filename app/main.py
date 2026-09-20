"""FastAPI application entry point.

Run locally:  uvicorn app.main:app --reload
Run in Docker: docker compose up --build
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .config import get_settings
from .db import SessionLocal
from .errors import DomainError
from .routers import decisions, instances, templates
from .scheduler import scheduler
from .seed import seed_demo

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("wf")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    if settings.scheduler_enabled:
        scheduler.start()
    logger.info("workflow engine started")
    yield
    scheduler.stop()
    logger.info("workflow engine stopped")


app = FastAPI(
    title="流程编排引擎",
    description=(
        "基于 FastAPI + SQLAlchemy + PostgreSQL 的审批流程引擎。"
        "支持 JSON 流程定义、会签/任签、条件分支、版本不可变、"
        "幂等审批、撤回与超时升级。\n\n"
        "重复请求使用相同 `request_id` 会返回首次结果（响应中 `replayed=true`）。"
    ),
    version="1.0.0",
    lifespan=lifespan,
)


@app.exception_handler(DomainError)
async def domain_error_handler(request: Request, exc: DomainError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message, "details": exc.details}},
    )


@app.get("/health", tags=["system"])
def health():
    return {"status": "ok"}


@app.post("/demo/seed", tags=["system"], status_code=201)
def run_seed():
    """Idempotently create the published demo template 'expense'."""
    db = SessionLocal()
    try:
        seed_demo(db)
    finally:
        db.close()
    return {"status": "seeded"}


app.include_router(templates.router)
app.include_router(instances.router)
app.include_router(decisions.router)
