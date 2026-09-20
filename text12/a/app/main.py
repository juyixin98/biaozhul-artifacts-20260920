from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app import lease_service
from app.config import settings
from app.db import SessionLocal
from app.routers import admin, device

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("cloudgate")


async def _reaper_loop() -> None:
    """后台租约回收：定时清理心跳超时的活动租约。"""
    interval = settings.reaper_interval_seconds
    while True:
        await asyncio.sleep(interval)
        try:
            await asyncio.to_thread(_run_reap)
        except Exception:  # noqa: BLE001
            logger.exception("租约回收周期执行失败")


def _run_reap() -> None:
    db = SessionLocal()
    try:
        reaped = lease_service.reap_expired()
        if reaped:
            logger.info("回收过期租约 %d 条", len(reaped))
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动即执行一次回收：服务重启后，超过心跳超时的活动租约被清理，地址归还地址池
    await asyncio.to_thread(_startup_recovery)
    task = None
    if settings.reaper_interval_seconds > 0:
        task = asyncio.create_task(_reaper_loop())
        logger.info("租约回收任务已启动，间隔 %ss", settings.reaper_interval_seconds)
    try:
        yield
    finally:
        if task:
            task.cancel()


def _startup_recovery() -> None:
    # 等待数据库就绪（容器首次启动时 postgres 可能仍在初始化）
    from sqlalchemy import text
    from sqlalchemy.exc import OperationalError

    db = SessionLocal()
    for attempt in range(30):
        try:
            db.execute(text("SELECT 1"))
            break
        except OperationalError:
            db.rollback()
            logger.info("等待数据库就绪（%s/30）", attempt + 1)
            time.sleep(1)
    else:
        raise RuntimeError("数据库不可用")
    try:
        reaped = lease_service.reap_expired()
        logger.info("启动恢复完成，回收过期租约 %d 条", len(reaped))
    finally:
        db.close()


app = FastAPI(
    title="CloudGate 多租户接入控制平面",
    version="1.0.0",
    description="模拟 VPN 接入控制：租户/接入点/地址池/设备管理，会话租约、心跳与撤销。"
                "不建立真实 VPN，不修改主机路由。",
    lifespan=lifespan,
)


@app.exception_handler(lease_service.ServiceError)
async def service_error_handler(request: Request, exc: lease_service.ServiceError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.http_status,
        content={"error": exc.code, "message": exc.message},
    )


@app.get("/health", tags=["system"])
def health() -> dict:
    return {"status": "ok", "service": "cloudgate-control-plane"}


app.include_router(admin.router)
app.include_router(device.router)
