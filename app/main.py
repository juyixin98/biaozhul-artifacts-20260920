import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.api import decisions, demo, instances, templates
from app.config import get_settings
from app.errors import ApiError
from app.worker import escalation_worker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    stop_event = asyncio.Event()
    task = None
    if get_settings().enable_worker:
        task = asyncio.create_task(escalation_worker(stop_event))
    try:
        yield
    finally:
        stop_event.set()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)


app = FastAPI(
    title="流程编排引擎 Workflow Orchestration Engine",
    version="1.0.0",
    description=(
        "JSON-defined approval workflows with immutable versioned templates, "
        "全签/任签 approvals, conditional branches, idempotent decisions, "
        "withdrawal and timeout escalation. No message queue required."
    ),
    lifespan=lifespan,
)


@app.exception_handler(ApiError)
async def api_error_handler(request, exc: ApiError):
    return JSONResponse(status_code=exc.status_code, content=exc.detail)


app.include_router(templates.router)
app.include_router(instances.router)
app.include_router(decisions.router)
app.include_router(demo.router)
