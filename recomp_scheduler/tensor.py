"""张量类型与记账式内存工作区（Arena）。

工程边界
--------
本项目不直接依赖真实 GPU 分配器，因此 :class:`Tensor` 是对 ``numpy.ndarray``
的轻量封装，并通过 :class:`Arena` 以"元素数"为单位记账：

- ``live_elements``   当前存活的激活/梯度张量占用；
- ``peak_elements``   运行期高水位（含临时工作区），这是与 planner 静态预测
                      对账的**运行时事实来源**；
- ``workspace_*``     算子执行期间显式登记的临时工作区，执行后必须释放，
                      否则 Arena 在关闭时抛出状态冲突。

张量在被 :meth:`Arena.release` 后进入 ``freed`` 状态；对已释放张量做运算会
抛出 :class:`~recomp_scheduler.errors.StateConflictError`，以暴露
"删掉必要激活后才发现"这类缺陷。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

import numpy as np

from .errors import BudgetInfeasibleError, ComputeFailureError, StateConflictError


@dataclass
class Tensor:
    """具名张量：值 + 形状 + 内存记账元数据。

    两个张量可能共享同一个 ndarray（例如分支图中的共享子图输出），
    但内存只按 :attr:`ref_count` 记一次，由 Arena 维护引用计数。
    """

    name: str
    data: np.ndarray
    arena: "Arena | None" = field(default=None, repr=False)
    ref_count: int = 1
    freed: bool = False
    kind: str = "activation"  # "activation" | "gradient" | "workspace" | "param"

    @property
    def shape(self) -> tuple[int, ...]:
        return tuple(int(s) for s in self.data.shape)

    @property
    def elements(self) -> int:
        return int(self.data.size)

    def require_live(self) -> None:
        if self.freed:
            raise StateConflictError(
                f"tensor {self.name!r} has already been freed",
                tensor=self.name,
                kind=self.kind,
            )


class Arena:
    """线程安全的内存记账工作区。

    所有 Tensor 的创建/释放都经过 Arena；此外算子可以通过
    :meth:`enter_workspace` 申请临时工作区，工作区生命周期计入峰值。

    >>> arena = Arena()
    >>> t = arena.allocate("x", np.zeros((3, 4)))
    >>> arena.live_elements
    12
    >>> arena.release(t)
    >>> arena.live_elements
    0
    >>> arena.peak_elements
    12
    """

    def __init__(self, budget_elements: int | None = None) -> None:
        self._lock = threading.RLock()
        self._live: dict[str, Tensor] = {}
        self._live_elements = 0
        self._peak_elements = 0
        self._workspace_elements = 0
        self._peak_workspace = 0
        self._open_workspaces = 0
        self._closed = False
        self._budget = None if budget_elements is None else int(budget_elements)
        self._allocation_log: list[tuple[str, str, int]] = []  # (action, name, size)

    # ---- 基本属性 -------------------------------------------------------
    @property
    def live_elements(self) -> int:
        with self._lock:
            return self._live_elements

    @property
    def peak_elements(self) -> int:
        """运行时高水位 = 存活张量峰值与(存活+工作区)峰值的较大者。"""
        with self._lock:
            return max(self._peak_elements, self._peak_workspace)

    @property
    def workspace_elements(self) -> int:
        with self._lock:
            return self._workspace_elements

    @property
    def peak_live_only(self) -> int:
        """不含工作区的存活峰值（用于分项对账）。"""
        with self._lock:
            return self._peak_elements

    @property
    def allocation_log(self) -> list[tuple[str, str, int]]:
        with self._lock:
            return list(self._allocation_log)

    # ---- 张量生命周期 ---------------------------------------------------
    def allocate(
        self,
        name: str,
        data: np.ndarray,
        *,
        kind: str = "activation",
    ) -> Tensor:
        """登记一张新张量。重名（未释放）属于状态冲突。"""
        with self._lock:
            if self._closed:
                raise StateConflictError("arena is closed", arena="closed")
            if name in self._live:
                raise StateConflictError(
                    f"tensor {name!r} already live in arena",
                    tensor=name,
                )
            tensor = Tensor(name=name, data=np.asarray(data), arena=self, kind=kind)
            self._live[name] = tensor
            self._live_elements += tensor.elements
            self._refresh_peak()
            self._allocation_log.append(("alloc", name, tensor.elements))
            return tensor

    def wrap(self, name: str, data: np.ndarray, *, kind: str = "activation") -> Tensor:
        """allocate 的语义别名（语义上表示"包装外部已有的 ndarray"）。"""
        return self.allocate(name, data, kind=kind)

    def retain(self, tensor: Tensor, holder: str = "") -> Tensor:
        """共享子图输出被多个消费者持有时增加引用计数。

        释放时必须与 retain 配对调用 :meth:`release`，否则 close 时报账不平。
        """
        with self._lock:
            tensor.require_live()
            tensor.ref_count += 1
            self._allocation_log.append(("retain", f"{tensor.name}<-{holder}", 0))
            return tensor

    def release(self, tensor: Tensor) -> None:
        with self._lock:
            if tensor.freed:
                raise StateConflictError(
                    f"tensor {tensor.name!r} released twice",
                    tensor=tensor.name,
                )
            tensor.ref_count -= 1
            self._allocation_log.append(("release", tensor.name, 0))
            if tensor.ref_count > 0:
                return
            if tensor.ref_count < 0:
                raise StateConflictError(
                    f"tensor {tensor.name!r} released more than retained",
                    tensor=tensor.name,
                    ref_count=tensor.ref_count,
                )
            tensor.freed = True
            self._live_elements -= tensor.elements
            self._live.pop(tensor.name, None)

    # ---- 临时工作区 -----------------------------------------------------
    def enter_workspace(self, elements: int, label: str = "tmp") -> "_Workspace":
        with self._lock:
            if self._closed:
                raise StateConflictError("arena is closed", arena="closed")
            if elements < 0:
                raise ComputeFailureError(
                    "negative workspace requested", elements=elements, label=label
                )
            ws = _Workspace(self, int(elements), label)
            self._workspace_elements += elements
            self._open_workspaces += 1
            self._refresh_peak()
            self._allocation_log.append(("workspace_enter", label, elements))
            return ws

    def _exit_workspace(self, ws: "_Workspace") -> None:
        with self._lock:
            self._workspace_elements -= ws.elements
            self._open_workspaces -= 1
            self._allocation_log.append(("workspace_exit", ws.label, ws.elements))

    # ---- 关闭与校验 -----------------------------------------------------
    def close(self) -> dict[str, int]:
        """关闭 Arena 并返回最终账目；若有泄漏则抛状态冲突。"""
        with self._lock:
            if self._open_workspaces != 0:
                raise StateConflictError(
                    "unclosed workspaces at arena close",
                    open_workspaces=self._open_workspaces,
                )
            leaked = [t.name for t in self._live.values()]
            if leaked:
                raise StateConflictError(
                    "live tensors leaked at arena close", tensors=leaked
                )
            self._closed = True
            return {
                "peak_elements": self.peak_elements,
                "peak_live_only": self._peak_elements,
            }

    # ---- 内部 -----------------------------------------------------------
    def _refresh_peak(self) -> None:
        if self._live_elements > self._peak_elements:
            self._peak_elements = self._live_elements
        total = self._live_elements + self._workspace_elements
        if total > self._peak_workspace:
            self._peak_workspace = total
        # 运行时预算是硬约束：一旦高水位越界立即判定资源耗尽，
        # 不依赖事后检查（事后无法区分是哪个分配导致的越界）。
        if self._budget is not None:
            current = max(self._live_elements, total)
            if current > self._budget:
                raise BudgetInfeasibleError(
                    "runtime memory budget exceeded",
                    phase="runtime",
                    live_elements=self._live_elements,
                    workspace_elements=self._workspace_elements,
                    current_total=current,
                    budget=self._budget,
                    over_by=current - self._budget,
                    peak_so_far=self.peak_elements,
                )


@dataclass
class _Workspace:
    arena: Arena
    elements: int
    label: str

    def __enter__(self) -> "_Workspace":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.arena._exit_workspace(self)


def finite(t: Tensor) -> bool:
    """数值守卫：张量中不得含 NaN/Inf。"""
    return bool(np.isfinite(t.data).all())


def require_finite(t: Tensor, where: str) -> None:
    if not finite(t):
        n_bad = int((~np.isfinite(t.data)).sum())
        raise ComputeFailureError(
            f"non-finite values in tensor {t.name!r} at {where}",
            tensor=t.name,
            where=where,
            bad_elements=n_bad,
        )
