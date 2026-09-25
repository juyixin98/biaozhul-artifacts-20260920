"""两个执行器：不复用基线与基于规划的复用执行器。

两者都按 op 的 time 顺序执行，且严格采用同一求值次序：

1. 先取出全部输入数组的引用；
2. 调用 NumPy 计算，结果是一块 *全新的* 临时数组；
3. 再把结果写回输出槽位。

因此即使规划把“在本 op 读完即死亡”的输入槽位分配给本 op 的输出
（端点相接复用），写入也发生在计算完成之后，matmul 这类非逐元素
算子同样安全。slice 不产生拷贝，只注册共享内存的视图。

内存口径（两个执行器一致）：峰值按 *静态生命周期模型* 计算——
半开区间 ``[birth, last_use)``，在事件点 t 存活当且仅当
``start <= t < end``；在 t 读完即死的值与在 t 出生的值可共享槽位。
基线为每个值（含切片拷贝）各占一块独立存储；复用执行器按规划共享。
NumPy 内核会自行临时分配输出缓冲，故进程瞬时 RSS 可能比静态峰值
多出一个 op 输出（已在 README 说明）；静态峰值才是本规划器的预算保证。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Mapping

import numpy as np

from .dag import DAG, Op
from .liveness import value_intervals
from .planner import Plan, naive_peak_live_elements, naive_total_elements

DTYPE = np.float64
ITEMSIZE = np.dtype(DTYPE).itemsize


@dataclass
class ExecutionResult:
    """一次执行的输出与静态内存占用。"""

    outputs: Dict[str, np.ndarray]
    peak_bytes: int
    peak_elements: int
    peak_time: int
    # 执行过程中累计申请的元素数：
    # 基线=每个值各一块（切片也复制）；复用=单块 arena 的长度
    total_allocated_elements: int
    # 逐事件点存活字节（静态模型），便于审计
    live_bytes_per_time: Dict[int, int] = field(default_factory=dict)


def _check_feeds(dag: DAG, feeds: Mapping[str, np.ndarray]) -> None:
    missing = sorted(set(dag.inputs) - set(feeds))
    if missing:
        raise ValueError(f"缺少输入: {missing}")
    extra = sorted(set(feeds) - set(dag.inputs))
    if extra:
        raise ValueError(f"提供了多余的输入: {extra}")
    for name, shape in dag.inputs.items():
        arr = np.asarray(feeds[name])
        if tuple(arr.shape) != tuple(shape):
            raise ValueError(
                f"输入 {name!r} 形状不符: 期望 {shape}, 实际 {arr.shape}"
            )


def _value_live_profile(dag: DAG) -> tuple[Dict[int, int], int, int]:
    """值级静态存活画像：返回 (逐事件点存活元素数, 峰值元素数, 峰值时刻)。

    每个值（含切片，基线中切片是独立拷贝）各计一整块。
    """
    iv = value_intervals(dag)
    points = sorted({i.start for i in iv.values()} | {i.end for i in iv.values()})
    per_time = {
        t: sum(
            dag.num_elements(name)
            for name, interval in iv.items()
            if interval.start <= t < interval.end
        )
        for t in points
    }
    peak_elem = naive_peak_live_elements(dag)
    peak_time = min(t for t, n in per_time.items() if n == peak_elem)
    return per_time, peak_elem, peak_time


def _release_dead(
    dag: DAG, env: Dict[str, np.ndarray], time: int
) -> None:
    """释放 time 时刻读完即死、且不是图输出的值引用（输入也可释放）。"""
    for name in list(env):
        if dag.is_output(name):
            continue
        if dag.last_use(name) == time:
            env.pop(name, None)


class NaiveExecutor:
    """不复用基线：每个值一块独立的新缓冲；slice 复制数据。

    值在最后使用点之后即不再被引用（允许释放、只是永不与他人共享），
    峰值按静态生命周期统计。
    """

    def __init__(self, dag: DAG) -> None:
        self.dag = dag

    def run(self, feeds: Mapping[str, np.ndarray]) -> ExecutionResult:
        _check_feeds(self.dag, feeds)
        env: Dict[str, np.ndarray] = {}
        for name, shape in self.dag.inputs.items():
            env[name] = np.array(feeds[name], dtype=DTYPE, copy=True).reshape(
                shape
            )

        for op in self.dag.ops:
            inputs = [env[name] for name in op.inputs]  # 先取引用
            env[op.name] = self._compute(op, inputs)  # 全新结果再发布
            _release_dead(self.dag, env, op.time)

        outputs = {
            name: np.array(env[name], copy=True)
            for name in self.dag.values
            if self.dag.is_output(name) and name in env
        }

        per_time_elem, peak_elem, peak_time = _value_live_profile(self.dag)
        return ExecutionResult(
            outputs=outputs,
            peak_bytes=peak_elem * ITEMSIZE,
            peak_elements=peak_elem,
            peak_time=peak_time,
            total_allocated_elements=naive_total_elements(self.dag),
            live_bytes_per_time={
                t: n * ITEMSIZE for t, n in per_time_elem.items()
            },
        )

    @staticmethod
    def _compute(op: Op, inputs: list[np.ndarray]) -> np.ndarray:
        if op.op_type == "add":
            return inputs[0] + inputs[1]
        if op.op_type == "matmul":
            return inputs[0] @ inputs[1]
        # slice：基线复制成独立缓冲，不做别名共享
        indexer = tuple(
            slice(start, start + size)
            for start, size in zip(op.starts, op.sizes)
        )
        return np.array(inputs[0][indexer], copy=True)


class ReuseExecutor:
    """基于 :class:`Plan` 在单块 arena 上执行，切片为真实内存别名。"""

    def __init__(self, dag: DAG, plan: Plan) -> None:
        self.dag = dag
        self.plan = plan

    def run(self, feeds: Mapping[str, np.ndarray]) -> ExecutionResult:
        _check_feeds(self.dag, feeds)
        arena = np.zeros(self.plan.arena_size, dtype=DTYPE)
        views: Dict[str, np.ndarray] = {}
        self._load_inputs(arena, views, feeds)

        for op in self.dag.ops:
            inputs = [views[name] for name in op.inputs]  # 先取引用
            self._publish(arena, views, op, inputs)  # 算完再写回
            _release_dead(self.dag, views, op.time)

        outputs = {
            name: np.array(views[name], copy=True)
            for name in self.dag.values
            if self.dag.is_output(name)
        }
        return self._result(outputs)

    def _load_inputs(
        self,
        arena: np.ndarray,
        views: Dict[str, np.ndarray],
        feeds: Mapping[str, np.ndarray],
    ) -> None:
        """把输入拷入各自槽位并建立 reshape 视图。"""
        for name, shape in self.dag.inputs.items():
            slot = self.plan.placement_of(name)
            buf = arena[slot.offset : slot.offset + slot.size].reshape(shape)
            buf[...] = np.asarray(feeds[name], dtype=DTYPE)
            views[name] = buf

    def _publish(
        self,
        arena: np.ndarray,
        views: Dict[str, np.ndarray],
        op: Op,
        inputs: list[np.ndarray],
    ) -> None:
        """计算 op 并把结果写回输出槽位（计算先于写回）。"""
        if op.op_type in ("add", "matmul"):
            result = (
                inputs[0] + inputs[1]
                if op.op_type == "add"
                else inputs[0] @ inputs[1]
            )
            slot = self.plan.placement_of(op.name)
            out = arena[slot.offset : slot.offset + slot.size].reshape(op.shape)
            out[...] = result
            views[op.name] = out
            return
        # slice：零拷贝别名
        view = inputs[0][self.dag.view_slices(op.name)]
        if tuple(view.shape) != op.shape:  # pragma: no cover - 防御
            raise AssertionError(
                f"slice {op.name!r} 视图形状 {view.shape} 与声明 {op.shape} 不符"
            )
        views[op.name] = view

    def _result(self, outputs: Dict[str, np.ndarray]) -> ExecutionResult:
        per_time = {
            t: n * ITEMSIZE for t, n in self.plan.live_per_time.items()
        }
        return ExecutionResult(
            outputs=outputs,
            peak_bytes=self.plan.peak_live_elements * ITEMSIZE,
            peak_elements=self.plan.peak_live_elements,
            peak_time=self.plan.peak_time,
            total_allocated_elements=self.plan.arena_size,
            live_bytes_per_time=dict(sorted(per_time.items())),
        )


def max_abs_diff(
    a: Mapping[str, np.ndarray], b: Mapping[str, np.ndarray]
) -> float:
    """两个输出字典逐输出比较，返回全局最大绝对误差。"""
    if set(a) != set(b):
        raise ValueError(f"输出集合不一致: {sorted(a)} vs {sorted(b)}")
    worst = 0.0
    for name in a:
        if a[name].shape != b[name].shape:
            raise ValueError(
                f"输出 {name!r} 形状不一致: {a[name].shape} vs {b[name].shape}"
            )
        worst = max(worst, float(np.max(np.abs(a[name] - b[name]))))
    return worst
