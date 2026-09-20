from fastapi import FastAPI

from app.routers import budgets, journals, periods, reports

app = FastAPI(
    title="CivicLedger",
    description=(
        "市政财务记账后端：分录过账、预算占用、期间关闭。"
        "聚焦核心记账流程，不含薪资与采购，不宣称满足任何会计法规认证。"
    ),
    version="0.1.0",
)

app.include_router(journals.router)
app.include_router(budgets.router)
app.include_router(periods.router)
app.include_router(reports.router)


@app.get("/health")
def health():
    return {"status": "ok"}
