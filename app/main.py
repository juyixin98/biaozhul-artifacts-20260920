"""FastAPI 入口：离线固定优先级周期任务可调度性分析服务。

仅纯后端，无前端页面。启动:
    uvicorn app.main:app --host 127.0.0.1 --port 8000
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from .integrity import KEY_SOURCE, sign
from .model import (
    MAX_TASKS,
    MAX_TIME_VALUE,
    AnalysisRequest,
    AnalysisResponse,
    ModelInfo,
    TIE_BREAK_RULE,
    utilization_report,
)
from .rta import MAX_ITERATIONS, analyze_taskset
from .simulation import simulate_and_crosscheck

MODEL_NAME = "Fixed-Priority Periodic Tasks with Constrained Deadlines (Liu & Layland)"
ASSUMPTIONS = [
    "单处理器（单核），基于固定优先级的完全可抢占调度，调度/切换开销为 0",
    "任务相互独立：无释放依赖、无执行顺序依赖；唯一允许的共享资源效应是"
    "以 B_i 给出的一次阻塞上界（优先级继承/天花板协议语义）",
    "每个任务周期释放作业，相邻释放间隔恰为 T_i；相对截止期 D_i <= T_i",
    "C_i 为最坏情况执行时间上界，作业在释放后到完成前持续占用需求至多 C_i",
    "临界瞬时：所有高优先级任务与目标任务同时释放，且目标任务遭遇最大阻塞；"
    "此相位下的响应时间即最坏响应时间（RTA 经典结论）",
    "时间参数均为正整数 tick；优先级规则固定为速率单调 RM，"
    "同周期按输入顺序仲裁，不接受调用方自定义优先级",
]
FORMULA = (
    "R0 = C_i + B_i; "
    "R^(n+1) = C_i + B_i + Σ_{j∈hp(i)} ceil(R^(n)/T_j)·C_j; "
    "不动点且 R<=D 可调度；R>D 截止期错失；保护性步数耗尽判不收敛，不判成功"
)

app = FastAPI(
    title="实时任务可调度分析服务",
    version="1.0.0",
    description="离线固定优先级周期任务的响应时间分析（RTA）+ 离散调度交叉验证。",
)


@app.get("/", response_model=ModelInfo)
def root() -> ModelInfo:
    """服务与模型信息（模型假设在此显式声明）。"""
    return ModelInfo(
        model=MODEL_NAME,
        assumptions=ASSUMPTIONS,
        priority_policy="RM (Rate Monotonic; 周期越短优先级越高)",
        tie_break_rule=TIE_BREAK_RULE,
        formula=FORMULA,
        limits={
            "max_tasks": MAX_TASKS,
            "max_time_value": MAX_TIME_VALUE,
            "max_iterations_per_task": MAX_ITERATIONS,
        },
    )


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "hmac_key_source": KEY_SOURCE}


@app.post("/api/analyze", response_model=AnalysisResponse)
def analyze(req: AnalysisRequest) -> JSONResponse:
    """执行 RTA 分析并与离散事件调度参考交叉对照。

    违反模型的输入（D>T、非正参数、重复 id、自定义/未知优先级策略、
    多余字段等）在进入本处理函数前由 Pydantic 以 422 拒绝。
    """
    rta_results = analyze_taskset(req.tasks)
    sim = simulate_and_crosscheck(req.tasks, rta_results)
    schedulable = all(r.schedulable for r in rta_results)

    resp = AnalysisResponse(
        schedulable=schedulable,
        model=MODEL_NAME,
        assumptions=ASSUMPTIONS,
        priority_policy="RM",
        tie_break_rule=TIE_BREAK_RULE,
        tasks=rta_results,
        utilization=utilization_report(req.tasks),
        simulation=sim,
    )

    payload = resp.model_dump(by_alias=True, mode="json")
    digest, mac = sign(payload)
    payload["sha256"] = digest
    payload["hmac_sha256"] = mac
    # 交叉验证失败属于内部一致性事故：计算如实暴露，不静默伪装成功。
    return JSONResponse(status_code=200, content=payload)
