from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.clock import Clock
from app.errors import ConstraintViolationError, DomainError
from app.routers import caregivers, coordinators, offers, plans, tasks

app = FastAPI(
    title="CareForce Scheduling API",
    version="1.0.0",
    description=(
        "护理任务排班后端：任务生成、约束分配、超时重排。"
        "不含薪资、志愿者或医疗决策。"
    ),
)
app.state.clock = Clock()

app.include_router(plans.router)
app.include_router(tasks.router)
app.include_router(offers.router)
app.include_router(caregivers.router)
app.include_router(coordinators.router)


@app.exception_handler(DomainError)
async def domain_error_handler(request: Request, exc: DomainError):
    content = {"detail": exc.detail}
    if isinstance(exc, ConstraintViolationError):
        content["violations"] = exc.violations
    return JSONResponse(status_code=exc.status_code, content=content)


@app.get("/health")
def health():
    return {"status": "ok"}
