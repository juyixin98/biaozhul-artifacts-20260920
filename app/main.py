"""FastAPI 入口：离线调度候选分析服务。"""

from __future__ import annotations

from fastapi import FastAPI

from .models import AnalyzeRequest, AnalyzeResponse
from .scheduler import analyze

app = FastAPI(
    title="topology-scheduler-analyzer",
    description=(
        "离线 Kubernetes 调度候选分析（明确子集）。先硬约束过滤再软约束评分，"
        "资源按 request 计算。不连接任何真实集群，也不是完整的默认调度器。"
    ),
    version="0.1.0",
)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/analyze", response_model=AnalyzeResponse)
def analyze_endpoint(request: AnalyzeRequest) -> AnalyzeResponse:
    return analyze(request)
