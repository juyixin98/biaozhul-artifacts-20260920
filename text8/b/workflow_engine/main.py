import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from workflow_engine.api import admin, instances, templates
from workflow_engine.config import get_settings
from workflow_engine.database import SessionLocal
from workflow_engine.demo import seed_demo
from workflow_engine.errors import register_error_handlers
from workflow_engine.sweeper import TimeoutSweeper

logger = logging.getLogger("workflow_engine")

settings = get_settings()
sweeper = TimeoutSweeper(settings)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.seed_demo_on_start:
        session = SessionLocal()
        try:
            with session.begin():
                result = seed_demo(session)
            logger.info("Demo seed: %s", result)
        except Exception:
            logger.exception("Demo seeding failed")
        finally:
            session.close()

    if settings.enable_sweeper:
        sweeper.start()
    yield
    if settings.enable_sweeper:
        sweeper.stop()


app = FastAPI(
    title="流程编排引擎",
    description=(
        "JSON 定义的审批流程引擎：开始 / 审批（全签、任签）/ 条件分支 / 结束；"
        "模板版本不可变、实例版本隔离；审批幂等、会签竞争、撤回与超时升级。\n\n"
        "所有写操作通过 `X-User` 请求头标识当前用户。"
    ),
    version="1.0.0",
    lifespan=lifespan,
)

register_error_handlers(app)
app.include_router(admin.router)
app.include_router(templates.router)
app.include_router(instances.router)
