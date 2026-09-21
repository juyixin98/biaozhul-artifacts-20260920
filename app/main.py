import asyncio
import contextlib
from collections.abc import AsyncIterator

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.database import SessionLocal
from app.errors import SkillPulseError
from app.routers import admin, courses, programs, progress, users
from app.services.enrollments import sweep_expired_seats

settings = get_settings()


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    task: asyncio.Task | None = None
    if settings.enable_sweeper:
        task = asyncio.create_task(_sweeper_loop())
    try:
        yield
    finally:
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


async def _sweeper_loop() -> None:
    while True:
        await asyncio.sleep(settings.sweeper_interval_seconds)
        # Run the synchronous DB work in a worker thread so the event loop
        # is not blocked.
        await asyncio.to_thread(_run_sweep)


def _run_sweep() -> None:
    db = SessionLocal()
    try:
        sweep_expired_seats(db)
        db.commit()
    except Exception:  # pragma: no cover - background safety net
        db.rollback()
    finally:
        db.close()


app = FastAPI(
    title="SkillPulse Training Execution API",
    version="1.0.0",
    description=(
        "Backend for supervisor-authored versioned training programs, "
        "capacity-bounded enrolment with waitlist and 48h seat confirmation, "
        "ordered step progress and one-shot certificates."
    ),
    lifespan=lifespan,
)


@app.exception_handler(SkillPulseError)
def handle_skillpulse_error(request, exc: SkillPulseError):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message}},
    )


for router in (
    users.router,
    programs.router,
    courses.router,
    progress.router,
    admin.router,
):
    app.include_router(router)


@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok"}
