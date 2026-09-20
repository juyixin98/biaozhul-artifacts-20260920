"""应用入口：``uvicorn app.main:app``。

启动时在同一进程内拉起超时升级 worker 线程（可用 WORKER_ENABLED=false 关闭）。
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import app as api_app
from app.config import settings
from app.worker import EscalationWorker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

_worker: EscalationWorker | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _worker
    if settings.worker_enabled:
        _worker = EscalationWorker()
        _worker.start()
    yield
    if _worker is not None:
        _worker.stop()


api_app.router.lifespan_context = lifespan

app = api_app
