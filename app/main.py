from fastapi import FastAPI

from .routers import audit, consents, policies, subjects

app = FastAPI(
    title="ConsentVault",
    description=(
        "可追溯的同意记录后端：按组织/主体/目的记录授予、撤回与到期，"
        "不可变事件历史 + 可重建状态投影。"
        "本系统只提供可追溯的授权状态记录，不宣称满足任何具体法规认证。"
    ),
    version="1.0.0",
)

app.include_router(consents.router)
app.include_router(policies.router)
app.include_router(subjects.router)
app.include_router(audit.router)


@app.get("/health")
def health():
    return {"status": "ok"}
