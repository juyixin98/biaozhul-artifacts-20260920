"""固定优先级响应时间分析（Response-Time Analysis, RTA）核心。

模型（在 API 文档与 README 中同样声明）:
  * 单核、完全抢占、基于固定优先级的调度；
  * 独立的周期性偶发任务（independent periodic/sporadic tasks），
    无释放依赖、无抖动；blocking B_i 仅建模优先级继承/天花板协议下
    “被低优先级任务临界区阻塞至多一次”的优先级反转上界；
  * 截止期受限 D_i <= T_i，临界瞬时取所有高优先级任务与任务 i 同时释放
    （且任务 i 恰好遭遇一次最大阻塞）；
  * 所有时间参数为正整数 tick。

迭代式（标准 RTA）:
    R_i^(0) = C_i + B_i
    R_i^(n+1) = C_i + B_i + Σ_{j ∈ hp(i)} ceil(R_i^(n) / T_j) · C_j

判定（关键：f(x) 关于 x 单调不减，故 R 序列单调不减）:
  * 不动点 R^(n+1) = R^(n) 且 R <= D_i        → 可调度（converged）；
  * 首次出现 R^(n) > D_i                       → 截止期错失（deadline_miss），
    报该迭代值与失败幅度 R-D；不再继续迭代，因为单调序列不可能回落，
    继续算只会浪费资源，且“错过截止期”这一结论已确定；
  * 步数耗尽仍未出现不动点也未超过 D_i          → non_converged，绝不判成功。
    由于每步 R 严格递增至少 1（整数），从初值 C+B 出发至多 D_i-(C+B)+1 步
    必触达 D_i+1，因此在正常输入范围内该分支只可能由保护性上限触发。
"""

from __future__ import annotations

from dataclasses import dataclass

from .model import (
    TaskInput,
    TaskResult,
    IterationStep,
    InterferenceSource,
)

# 保护性步数上限：理论上 <= max(D)+1 足够（每步至少 +1），这里取与
# 输入参数硬上界一致的量级；正常任务集通常在个位数步数内收敛。
MAX_ITERATIONS = 1_000_500


@dataclass(frozen=True)
class PriorityOrderedTask:
    task: TaskInput
    rank: int  # 1 = 最高优先级


def assign_priorities(tasks: list[TaskInput]) -> list[PriorityOrderedTask]:
    """速率单调（RM）赋优先级：周期越短优先级越高。

    同优先级仲裁规则固定：周期相同按输入顺序（先出现者优先级更高）。
    sorted 稳定，因此 (T 升序, 输入下标升序) 即完整规则。
    """
    ordered = sorted(enumerate(tasks), key=lambda pair: (pair[1].period, pair[0]))
    return [PriorityOrderedTask(task=t, rank=i + 1) for i, (_idx, t) in enumerate(ordered)]


def _ceil_div(a: int, b: int) -> int:
    return -(-a // b)


def analyze_task(
    target: PriorityOrderedTask,
    higher: list[PriorityOrderedTask],
) -> TaskResult:
    """对单个任务执行 RTA 不动点迭代。"""
    t = target.task
    hp = [h.task for h in higher]
    base = t.wcet + t.blocking

    steps: list[IterationStep] = []
    outcome: str
    final_rt: int | None = None
    miss: int | None = None

    # 第 0 步：初值 R0 = C_i + B_i（无高优先级干扰时的下界）。
    steps.append(
        IterationStep(
            step=0,
            r_prev=0,
            total_interference=0,
            r_next=base,
            interferences=[
                InterferenceSource(
                    task_id=j.id,
                    period=j.period,
                    wcet=j.wcet,
                    releases_in_last_rt=0,
                    interference=0,
                )
                for j in hp
            ],
        )
    )

    # 初值就已超过截止期（C+B>D）：直接失败，第 0 步即证据。
    r_prev = base
    if r_prev > t.deadline:
        outcome = "deadline_miss"
        final_rt = base
        miss = base - t.deadline
        return TaskResult(
            task_id=t.id,
            wcet=t.wcet,
            period=t.period,
            deadline=t.deadline,
            blocking=t.blocking,
            priority_rank=target.rank,
            higher_priority_tasks=[j.id for j in hp],
            iterations=steps,
            final_rt=final_rt,
            outcome=outcome,
            schedulable=False,
            deadline_miss=miss,
        )

    settled = False
    for n in range(1, MAX_ITERATIONS + 1):
        inter: list[InterferenceSource] = []
        total = 0
        for j in hp:
            releases = _ceil_div(r_prev, j.period)
            amount = releases * j.wcet
            total += amount
            inter.append(
                InterferenceSource(
                    task_id=j.id,
                    period=j.period,
                    wcet=j.wcet,
                    releases_in_last_rt=releases,
                    interference=amount,
                )
            )
        r_next = base + total

        steps.append(
            IterationStep(
                step=n,
                r_prev=r_prev,
                total_interference=total,
                r_next=r_next,
                interferences=inter,
            )
        )

        if r_next == r_prev:  # 不动点：此前已保证 r_prev <= D
            outcome = "converged"
            final_rt = r_next
            settled = True
            break
        if r_next < r_prev:
            # f 单调，理论上不可能出现；出现即模型/实现被破坏，按不收敛处理。
            outcome = "non_converged"
            settled = True
            break
        if r_next > t.deadline:  # 单调序列首次越过截止期 → 确定性错失
            outcome = "deadline_miss"
            final_rt = r_next
            miss = r_next - t.deadline
            settled = True
            break
        r_prev = r_next

    if not settled:
        outcome = "non_converged"

    return TaskResult(
        task_id=t.id,
        wcet=t.wcet,
        period=t.period,
        deadline=t.deadline,
        blocking=t.blocking,
        priority_rank=target.rank,
        higher_priority_tasks=[j.id for j in hp],
        iterations=steps,
        final_rt=final_rt,
        outcome=outcome,
        schedulable=outcome == "converged",
        deadline_miss=miss,
    )


def analyze_taskset(tasks: list[TaskInput]) -> list[TaskResult]:
    """对整个任务集按优先级从高到低分析（低优先级任务的 hp 集合即其前缀）。"""
    ordered = assign_priorities(tasks)
    results: list[TaskResult] = []
    for k, pot in enumerate(ordered):
        results.append(analyze_task(pot, ordered[:k]))
    # 按输入顺序返回，便于调用方对照
    by_id = {r.task_id: r for r in results}
    return [by_id[t.id] for t in tasks]
