"""离散事件调度参考实现（独立于 RTA 迭代，用于交叉验证）。

仿真在“临界瞬时”相位下进行：所有任务的首个作业在 t=0 同时释放，
之后按各自周期释放新作业。调度器为单核、固定优先级、完全抢占；
不存在同优先级任务（RM + 固定仲裁后每个任务有唯一 rank）。

两种观察方式:
  1. 全任务集独立作业调度（global schedule）：不注入阻塞，透明展示每个
     作业的 release/start/finish/response 与截止期错失、horizon 末积压；
  2. 逐任务阻塞仿真（blocking emulation）：对目标任务 τ_i，在 t=0 放入
     一个“持锁低优先级任务”，它需要 B_i 个执行单位、有效优先级等于 τ_i
     （即 hp(i) 可抢占它，τ_i 必须等它执行完毕才能运行）。这正是优先级
     继承协议下阻塞上界进入 RTA 递推的语义，因此 τ_i 首作业的离散仿真
     响应时间应与 RTA 不动点 R_i **精确相等**（B_i=0 时两种方式等价）。

horizon 取超周期与安全上限的较小者，防止周期 lcm 爆炸；对利用率>1
的发散任务集，目标作业在 horizon 内无法完成即与 RTA non_converged 对应。
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field

from .model import (
    MAX_TIME_VALUE,
    TaskInput,
    SimulationReport,
    SimulationJob,
    TaskResult,
)
from .rta import PriorityOrderedTask, assign_priorities

HORIZON_CAP = 200_000          # 全局调度观察窗口上限（tick）
BLOCKING_SIM_CAP = 2_000_001   # 逐任务阻塞仿真观察窗口上限（> 最大可能 D）


def _hyperperiod(periods: list[int]) -> int:
    from math import lcm

    h = 1
    for p in periods:
        h = lcm(h, p)
        if h > HORIZON_CAP:
            return HORIZON_CAP
    return min(h, HORIZON_CAP)


@dataclass
class _Job:
    key: tuple[float, int]  # (优先级 rank, 次级仲裁)
    task_id: str
    seq: int
    release: int
    remain: int
    deadline: int
    start: int | None = None
    finish: int | None = None
    is_holder: bool = False


@dataclass
class _TaskSpec:
    key: tuple[float, int]
    task_id: str
    period: int
    wcet: int
    deadline: int


def _simulate(
    specs: list[_TaskSpec],
    horizon: int,
    drain_cap: int,
    watch_task: str | None = None,
    stop_when_watch_past_deadline: bool = False,
) -> tuple[list[_Job], dict[str, _Job], str]:
    """事件驱动抢占调度。

    返回 (全部作业, watch 首作业表, 状态)。
    状态: "completed" 正常结束；"overdue" 被观察作业到截止期仍未完成；
    "timeout" 到达时间窗口上界仍有作业未完成。

    释放严格位于 [0, horizon)；drain 阶段不再释放新作业，只排空队列，
    最多再执行 drain_cap 个 tick。
    """
    # 释放事件堆 (time, tie, spec_index)
    release_heap: list[tuple[int, int, int]] = []
    for si, sp in enumerate(specs):
        if sp.period > 0:
            release_heap.append((0, si, si))
    heapq.heapify(release_heap)

    ready: list[tuple] = []  # 堆条目 (*key, seq, job)
    active: set[int] = set()  # 仍待运行（含正在运行）作业的 seq
    all_jobs: list[_Job] = []
    seq_counter = 0
    now = 0
    watched: dict[str, _Job] = {}
    seen_task_ids: set[str] = set()
    status = "completed"

    def release_due(upto: int) -> None:
        nonlocal seq_counter
        while release_heap and release_heap[0][0] <= upto:
            t, _tie, si = heapq.heappop(release_heap)
            sp = specs[si]
            job = _Job(
                key=sp.key,
                task_id=sp.task_id,
                seq=seq_counter,
                release=t,
                remain=sp.wcet,
                deadline=t + sp.deadline,
            )
            seq_counter += 1
            heapq.heappush(ready, (*job.key, job.seq, job))
            active.add(job.seq)
            all_jobs.append(job)
            # 观察目标注册为“该任务首次释放的作业”，与全局序号无关。
            if watch_task is not None and sp.task_id == watch_task:
                if sp.task_id not in seen_task_ids:
                    watched[sp.task_id] = job
                    seen_task_ids.add(sp.task_id)
            nxt = t + sp.period
            if nxt < horizon:
                heapq.heappush(release_heap, (nxt, si, si))

    def pick_running() -> _Job | None:
        # 惰性删除：每个作业只入堆一次，完成即从 active 移除；
        # 其堆条目升到堆顶时被弹出，永不重复选择。
        while ready:
            top = ready[0]
            job = top[3]
            if job.seq in active and job.remain > 0:
                return job
            heapq.heappop(ready)
        return None

    end_of_time = horizon + drain_cap
    while True:
        release_due(now)
        # 观察点必须在 release_due 之后：被观察作业在 t=0 的释放事件里创建。
        if watch_task is not None:
            wj = watched.get(watch_task)
            if wj is not None:
                if wj.finish is not None:
                    status = "completed"
                    break
                if stop_when_watch_past_deadline and now >= wj.deadline:
                    status = "overdue"
                    break
        job = pick_running()
        if job is None:
            if not release_heap:
                break
            jump_to = release_heap[0][0]
            # 空闲跳转不得越过被观察作业的截止期检查点。
            wj0 = watched.get(watch_task) if watch_task is not None else None
            if (
                stop_when_watch_past_deadline
                and wj0 is not None
                and wj0.finish is None
                and now < wj0.deadline <= jump_to
            ):
                now = wj0.deadline
                continue
            now = jump_to
            continue

        # 下一次事件：更高优先级作业释放，或当前作业完成
        run_until = now + job.remain
        for t, _tie, si in release_heap:
            if t > run_until:
                break
            if specs[si].key < job.key and t > now:
                run_until = t
                break
        run_until = min(run_until, end_of_time)
        wj1 = watched.get(watch_task) if watch_task is not None else None
        if (
            stop_when_watch_past_deadline
            and wj1 is not None
            and wj1.finish is None
            and now < wj1.deadline < run_until
        ):
            run_until = wj1.deadline

        if job.start is None:
            job.start = now
        if run_until <= now:
            # 时间无法推进且作业未完成（drain 超时）
            status = "timeout" if watch_task is not None else "completed"
            break
        job.remain -= run_until - now
        now = run_until
        if job.remain == 0:
            job.finish = now
            active.discard(job.seq)
            # 不主动 heappop：该条目此刻未必仍在堆顶（抢占后堆顶可能是更新的
            # 高优先级作业）；它升到堆顶时由 pick_running 惰性删除。
        if now >= end_of_time:
            status = "timeout" if watch_task is not None else "completed"
            break

    return all_jobs, watched, status


def _global_schedule(tasks: list[TaskInput]) -> tuple[list[SimulationJob], int, int, int]:
    ordered = assign_priorities(tasks)
    specs = [
        _TaskSpec(
            key=(float(o.rank), 0),
            task_id=o.task.id,
            period=o.task.period,
            wcet=o.task.wcet,
            deadline=o.task.deadline,
        )
        for o in ordered
    ]
    horizon = _hyperperiod([t.period for t in tasks])
    # drain_cap=0：释放窗口为 [0, horizon)，恰在 horizon 未完成的作业计为积压。
    # 对可调度任务集，超周期边界积压必为 0（末作业释放于 horizon-T，R<=D<=T）。
    jobs, _, _ = _simulate(specs, horizon, drain_cap=0)
    out: list[SimulationJob] = []
    misses = 0
    backlog = 0
    for j in jobs:
        if j.finish is None:
            backlog += 1
            continue
        missed = j.finish > j.deadline
        misses += int(missed)
        out.append(
            SimulationJob(
                task_id=j.task_id,
                release=j.release,
                start=j.start,
                finish=j.finish,
                response=j.finish - j.release,
                deadline=j.deadline,
                deadline_missed=missed,
            )
        )
    return out, horizon, misses, backlog


def _blocking_response_for(
    ordered: list[PriorityOrderedTask], idx: int
) -> tuple[int | None, str]:
    """逐目标任务的阻塞仿真。

    返回 (首作业响应时间, 状态)：
      ("completed", R) 作业在截止期前/后正常完成，R 为其响应时间；
      ("overdue", None) 到达截止期时刻作业仍未完成（确定错失）；
      ("timeout", None) 到达仿真窗口上界仍未完成。
    """
    target = ordered[idx]
    # hp 任务 + 持锁者 + 目标任务。持锁者 key 恰在目标之上、hp 之下。
    specs: list[_TaskSpec] = []
    for h in ordered[:idx]:
        specs.append(
            _TaskSpec(
                key=(float(h.rank), 0),
                task_id=h.task.id,
                period=h.task.period,
                wcet=h.task.wcet,
                deadline=h.task.period,
            )
        )
    if target.task.blocking > 0:
        specs.append(
            _TaskSpec(
                # 持锁者继承目标任务的优先级：与目标同 rank，但靠 tie-break=0
                # 排在目标之前（FIFO：它先进入临界区）；所有 hp 任务仍可抢占它。
                key=(float(target.rank), 0),
                task_id=f"__lock_holder_for_{target.task.id}__",
                period=BLOCKING_SIM_CAP + 1,  # 仅释放一次
                wcet=target.task.blocking,
                deadline=BLOCKING_SIM_CAP,
            )
        )
    specs.append(
        _TaskSpec(
            key=(float(target.rank), 1),
            task_id=target.task.id,
            period=target.task.period,
            wcet=target.task.wcet,
            deadline=target.task.deadline,
        )
    )
    _, watched, status = _simulate(
        specs,
        horizon=BLOCKING_SIM_CAP,
        drain_cap=0,
        watch_task=target.task.id,
        stop_when_watch_past_deadline=True,
    )
    j = watched.get(target.task.id)
    if status == "completed" and j is not None and j.finish is not None:
        return j.finish - j.release, "completed"
    return None, status


def simulate_and_crosscheck(
    tasks: list[TaskInput], rta_results: list[TaskResult]
) -> SimulationReport:
    """运行离散调度参考并与 RTA 结果逐项对照。"""
    jobs, horizon, misses, backlog = _global_schedule(tasks)

    max_resp: dict[str, int] = {}
    for j in jobs:
        max_resp[j.task_id] = max(max_resp.get(j.task_id, 0), j.response)

    ordered = assign_priorities(tasks)
    mismatch: str | None = None
    for idx, o in enumerate(ordered):
        r = next(x for x in rta_results if x.task_id == o.task.id)
        sim_r, sim_status = _blocking_response_for(ordered, idx)
        if r.outcome == "converged":
            # 可调度任务：两种独立方法必须给出完全相同的最坏响应时间。
            if sim_status != "completed" or sim_r is None:
                mismatch = (
                    f"任务 {r.task_id}: RTA 收敛到 R={r.final_rt}，"
                    f"但离散调度状态为 {sim_status}（未正常完成该作业）"
                )
                break
            if sim_r != r.final_rt:
                mismatch = (
                    f"任务 {r.task_id}: RTA 不动点 R={r.final_rt} 与离散调度"
                    f"临界瞬时响应 {sim_r} 不一致"
                )
                break
        elif r.outcome == "deadline_miss":
            # 截止期错失：离散调度必须同样显示 response > D（overdue）
            # 或在一个 >D 的有限值完成（completed 且 sim_r>D）。
            if sim_status == "overdue":
                continue
            if sim_status == "completed" and sim_r is not None and sim_r > o.task.deadline:
                continue
            mismatch = (
                f"任务 {r.task_id}: RTA 判定截止期错失（首个超限迭代值 "
                f"{r.final_rt} > D={o.task.deadline}），但离散调度状态="
                f"{sim_status}, response={sim_r}，两种方法冲突"
            )
            break
        else:  # non_converged：仅保护性上限触发，仿真不对此分支做一致性断言
            continue

    mode = (
        "critical_instant_phased; global=independent_jobs(no blocking); "
        "per_target=priority_inheritance_blocking_emulation"
    )
    return SimulationReport(
        horizon=horizon,
        mode=mode,
        jobs=jobs,
        max_response_per_task=max_resp,
        deadline_misses=misses,
        backlog_at_horizon=backlog,
        matches_rta=mismatch is None,
        mismatch=mismatch,
    )
