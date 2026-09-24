"""请求/响应数据模型与输入校验。

校验即模型约束的强制表达：违反 Liu & Layland 任务模型（或服务安全边界）
的输入在这里被拒绝，绝不进入分析流程。
"""

from __future__ import annotations

from fractions import Fraction
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

# ---- 服务安全边界（防止恶意/病态输入拖垮进程）-----------------------------
MAX_TASKS = 64
MAX_TIME_VALUE = 1_000_000  # 单个时间参数（C/T/D/B）的硬上限
MAX_ID_LEN = 64

PriorityPolicy = Literal["RM"]  # 唯一支持的固定优先级规则：速率单调
TIE_BREAK_RULE = "同周期（同优先级）按输入顺序仲裁，先出现者优先级更高"


class TaskInput(BaseModel):
    """单个周期任务的输入参数（全部为正整数时间单位，tick）。

    属性:
        id: 任务标识，集合内唯一。
        wcet: 最坏情况执行时间上界 C_i（>0）。
        period: 周期 T_i，也是任务作业释放间隔（>0）。
        deadline: 相对截止期 D_i，模型限定 D_i <= T_i。
        blocking: 阻塞上界 B_i（>=0），模拟非抢占临界区造成的最大优先级反转。
    """

    id: str = Field(..., min_length=1, max_length=MAX_ID_LEN)
    wcet: int = Field(..., alias="C", gt=0, le=MAX_TIME_VALUE)
    period: int = Field(..., alias="T", gt=0, le=MAX_TIME_VALUE)
    deadline: int = Field(..., alias="D", gt=0, le=MAX_TIME_VALUE)
    blocking: int = Field(0, alias="B", ge=0, le=MAX_TIME_VALUE)

    model_config = {"populate_by_name": True, "extra": "forbid"}

    @field_validator("id")
    @classmethod
    def _id_nonblank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("任务 id 不能为空字符串")
        return v


class AnalysisRequest(BaseModel):
    """分析请求：一组独立周期任务 + 固定的优先级规则标识。

    priority_policy 必须显式为 "RM"：同优先级仲裁规则固定为
    “周期升序，周期相同则按输入顺序”，不接受调用方提供的自定义优先级。
    """

    tasks: list[TaskInput] = Field(..., min_length=1, max_length=MAX_TASKS)
    priority_policy: PriorityPolicy = Field(
        default="RM",
        description="固定优先级规则。仅支持 RM（速率单调）；同周期按输入顺序仲裁。",
    )

    @model_validator(mode="after")
    def _validate_taskset(self) -> AnalysisRequest:
        ids = [t.id for t in self.tasks]
        if len(set(ids)) != len(ids):
            dup = sorted({i for i in ids if ids.count(i) > 1})
            raise ValueError(f"任务 id 必须唯一，发现重复: {dup}")
        violations: list[str] = []
        for t in self.tasks:
            if t.deadline > t.period:
                violations.append(
                    f"任务 {t.id}: D={t.deadline} > T={t.period}，违反 D_i <= T_i 模型约束"
                )
        if violations:
            raise ValueError("；".join(violations))
        return self


class InterferenceSource(BaseModel):
    """单个高优先级任务对目标任务的干扰明细。"""

    task_id: str
    period: int
    wcet: int
    releases_in_last_rt: int  # ceil(R_prev / T_j)
    interference: int  # ceil(R_prev / T_j) * C_j


class IterationStep(BaseModel):
    """RTA 第 n 步迭代快照（n 从 0 开始）。

    step=0 为初值 R_0 = C_i + B_i（无干扰下界）；
    step>=1 满足 R_n = C_i + B_i + Σ_{j∈hp(i)} ceil(R_{n-1}/T_j)·C_j。
    """

    step: int
    r_prev: int
    total_interference: int
    r_next: int
    interferences: list[InterferenceSource]


class TaskResult(BaseModel):
    task_id: str
    wcet: int = Field(serialization_alias="C")
    period: int = Field(serialization_alias="T")
    deadline: int = Field(serialization_alias="D")
    blocking: int = Field(serialization_alias="B")
    priority_rank: int  # 1 = 最高优先级
    higher_priority_tasks: list[str]
    iterations: list[IterationStep]
    final_rt: int | None = None
    # converged: 不动点收敛；deadline_miss: 收敛但 R>D；non_converged: 达到迭代上限仍未不动点
    outcome: Literal["converged", "deadline_miss", "non_converged"]
    schedulable: bool
    deadline_miss: int | None = None  # R - D，仅 deadline_miss 时有值


class UtilizationReport(BaseModel):
    total_utilization_fraction: str  # 精确分数 "p/q"
    total_utilization_float: float
    per_task: dict[str, str]
    liu_layland_bound_n_tasks: float  # n(2^(1/n)-1)，仅信息性参考
    note: str


class SimulationJob(BaseModel):
    task_id: str
    release: int
    start: int
    finish: int
    response: int
    deadline: int  # 绝对截止期
    deadline_missed: bool


class SimulationReport(BaseModel):
    """离散事件调度参考：关键瞬时（所有任务相位为 0）下的抢占式调度结果。"""

    horizon: int
    mode: str
    jobs: list[SimulationJob]
    max_response_per_task: dict[str, int]
    deadline_misses: int
    backlog_at_horizon: int
    matches_rta: bool
    mismatch: str | None = None


class AnalysisResponse(BaseModel):
    schedulable: bool
    model: str
    assumptions: list[str]
    priority_policy: str
    tie_break_rule: str
    tasks: list[TaskResult]
    utilization: UtilizationReport
    simulation: SimulationReport
    sha256: str | None = None  # 规范响应体（不含本字段与 hmac_sha256）的 SHA-256
    hmac_sha256: str | None = None  # 以服务端密钥对同一规范字节计算的 HMAC-SHA256


class ModelInfo(BaseModel):
    model: str
    assumptions: list[str]
    priority_policy: str
    tie_break_rule: str
    formula: str
    limits: dict[str, int]


def utilization_report(tasks: list[TaskInput]) -> UtilizationReport:
    """计算精确利用率（仅作信息性展示，不作为可调度判据）。"""
    total = Fraction(0)
    per: dict[str, str] = {}
    for t in tasks:
        f = Fraction(t.wcet, t.period)
        total += f
        per[t.id] = f"{f.numerator}/{f.denominator}"
    n = len(tasks)
    bound = n * (2.0 ** (1.0 / n) - 1.0) if n else 0.0
    return UtilizationReport(
        total_utilization_fraction=f"{total.numerator}/{total.denominator}",
        total_utilization_float=float(total),
        per_task=per,
        liu_layland_bound_n_tasks=bound,
        note=(
            "U<=1（或 U<=RM 充分界）既非可调度的充分条件也非必要判据；"
            "本服务仅以响应时间分析的不动点结果判定可调度性。"
        ),
    )
