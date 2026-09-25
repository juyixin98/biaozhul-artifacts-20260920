"""静态张量计算 DAG 的定义、构建与校验。

时间模型：把每个 op 绑定一个整数执行时刻 ``time``。约定
- 输入值在 ``time=0`` 即存活；
- op ``v = f(inputs)`` 在 ``time`` 时刻读取其输入并产出 ``v``，``v`` 从
  ``time`` 时刻开始存活，到其最后一次被读取的那个时刻结束；
- slice 视图视作独立的值，但与基底层共享存储（见 :mod:`tensor_planner.liveness`）。

只支持三种算子：
- ``add(a, b)``：形状相同逐元素相加；
- ``matmul(a, b)``：二维矩阵乘法 ``[m,k] x [k,n] -> [m,n]``；
- ``slice(src, starts, sizes)``：产生不重叠的切片视图，与源共享存储。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Sequence, Tuple

import numpy as np

SUPPORTED_OPS = ("add", "matmul", "slice")


class DAGValidationError(ValueError):
    """DAG 结构或形状不合法。"""


@dataclass(frozen=True)
class Op:
    """DAG 中的一个算子节点。"""

    name: str
    op_type: str
    inputs: Tuple[str, ...]
    shape: Tuple[int, ...]
    time: int
    # 仅 slice 使用：
    starts: Tuple[int, ...] = ()
    sizes: Tuple[int, ...] = ()

    @property
    def is_slice(self) -> bool:
        return self.op_type == "slice"

    @property
    def base(self) -> str:
        if not self.is_slice:
            raise AttributeError(f"op {self.name!r} 不是 slice")
        return self.inputs[0]


def _c_strides(shape: Sequence[int]) -> Tuple[int, ...]:
    strides: List[int] = []
    stride = 1
    for dim in reversed(shape):
        strides.append(stride)
        stride *= dim
    return tuple(reversed(strides))


@dataclass
class DAG:
    """不可变结构（构建完成后）的张量计算图。

    Attributes:
        inputs: 输入名 -> 形状。
        ops: 按 time 排序的算子列表。
        shapes: 全部值（输入与 op 输出）的形状。
        producer: 值 -> 生产它的 op（输入没有 producer）。
        consumers: 值 -> 读取它的 op 列表（按 time 排序）。
    """

    inputs: Dict[str, Tuple[int, ...]]
    ops: List[Op]
    shapes: Dict[str, Tuple[int, ...]]
    producer: Dict[str, Op] = field(default_factory=dict)
    consumers: Dict[str, List[Op]] = field(default_factory=dict)
    # 基底 -> 该基底上的全部切片视图（要求构成不重叠划分）
    slices: Dict[str, List[Op]] = field(default_factory=dict)
    # 值 -> 所属存储组的根（基底本身；普通值根即自身）
    root_of: Dict[str, str] = field(default_factory=dict)

    # ---- 基本查询 ----
    @property
    def values(self) -> List[str]:
        """全部值名：输入在前，其后按 op 顺序。"""
        return list(self.inputs) + [op.name for op in self.ops]

    def num_elements(self, name: str) -> int:
        return int(np.prod(self.shapes[name]))

    def is_view(self, name: str) -> bool:
        return self.root_of[name] != name

    def view_offset(self, name: str) -> int:
        """切片视图首元素相对基底首元素的 C 布局扁平偏移。"""
        producer = self.producer[name]
        base_shape = self.shapes[producer.base]
        strides = _c_strides(base_shape)
        return sum(s * st for s, st in zip(producer.starts, strides))

    def view_slices(self, name: str) -> Tuple[slice, ...]:
        producer = self.producer[name]
        return tuple(
            slice(start, start + size)
            for start, size in zip(producer.starts, producer.sizes)
        )

    def storage_size(self, root: str) -> int:
        """一个存储组根所需的元素数。"""
        return self.num_elements(root)

    def all_storage_roots(self) -> List[str]:
        roots: List[str] = []
        for name in self.values:
            if self.root_of[name] == name:
                roots.append(name)
        return roots

    def members_of(self, root: str) -> List[str]:
        return [name for name in self.values if self.root_of[name] == root]

    def last_use(self, name: str) -> int:
        """值最后一次被读取的时刻；图输出（无消费者）存活到程序结束。

        程序结束时刻为 :attr:`horizon`。从未被读取的非输出值（理论上
        只会是孤立节点）在其出生时刻死亡。
        """
        uses = self.consumers.get(name, ())
        if uses:
            return max(op.time for op in uses)
        if self.is_output(name):
            return self.horizon
        return self.birth(name)

    def birth(self, name: str) -> int:
        if name in self.inputs:
            return 0
        return self.producer[name].time

    def is_output(self, name: str) -> bool:
        return not self.consumers.get(name)

    @property
    def horizon(self) -> int:
        """程序结束时刻（半开）：最后一个 op 时刻 + 1；无 op 时为 1。"""
        if not self.ops:
            return 1
        return max(op.time for op in self.ops) + 1


class DAGBuilder:
    """逐步构建并校验 :class:`DAG`。"""

    def __init__(self) -> None:
        self._inputs: Dict[str, Tuple[int, ...]] = {}
        self._ops: List[Op] = []
        self._defined: set[str] = set()

    def add_input(self, name: str, shape: Sequence[int]) -> "DAGBuilder":
        shape = tuple(int(d) for d in shape)
        self._check_name(name)
        _check_shape(shape)
        self._inputs[name] = shape
        self._defined.add(name)
        return self

    def add_add(self, name: str, a: str, b: str, time: int) -> "DAGBuilder":
        self._check_name(name)
        self._require_defined(a, b)
        if self._shape(a) != self._shape(b):
            raise DAGValidationError(
                f"add {name!r} 输入形状不一致: {self._shape(a)} vs {self._shape(b)}"
            )
        self._ops.append(
            Op(name, "add", (a, b), self._shape(a), int(time))
        )
        self._defined.add(name)
        return self

    def add_matmul(
        self, name: str, a: str, b: str, time: int
    ) -> "DAGBuilder":
        self._check_name(name)
        self._require_defined(a, b)
        sa, sb = self._shape(a), self._shape(b)
        if len(sa) != 2 or len(sb) != 2:
            raise DAGValidationError(
                f"matmul {name!r} 只支持二维张量，得到 {sa} 和 {sb}"
            )
        if sa[1] != sb[0]:
            raise DAGValidationError(
                f"matmul {name!r} 内维不匹配: {sa} x {sb}"
            )
        out_shape = (sa[0], sb[1])
        self._ops.append(
            Op(name, "matmul", (a, b), out_shape, int(time))
        )
        self._defined.add(name)
        return self

    def add_slice(
        self,
        name: str,
        src: str,
        starts: Sequence[int],
        sizes: Sequence[int],
        time: int,
    ) -> "DAGBuilder":
        self._check_name(name)
        self._require_defined(src)
        base_shape = self._shape(src)
        starts_t = tuple(int(s) for s in starts)
        sizes_t = tuple(int(s) for s in sizes)
        if len(starts_t) != len(base_shape) or len(sizes_t) != len(base_shape):
            raise DAGValidationError(
                f"slice {name!r} 维度数与基底 {base_shape} 不一致"
            )
        for axis, (start, size, dim) in enumerate(
            zip(starts_t, sizes_t, base_shape)
        ):
            if start < 0 or size <= 0 or start + size > dim:
                raise DAGValidationError(
                    f"slice {name!r} 第 {axis} 维越界: "
                    f"start={start}, size={size}, dim={dim}"
                )
        self._ops.append(
            Op(
                name,
                "slice",
                (src,),
                sizes_t,
                int(time),
                starts=starts_t,
                sizes=sizes_t,
            )
        )
        self._defined.add(name)
        return self

    @staticmethod
    def _check_unique_times(ops: Sequence[Op]) -> None:
        seen: Dict[int, str] = {}
        for op in ops:
            if op.time in seen:
                raise DAGValidationError(
                    f"时刻 {op.time} 上有两个 op: "
                    f"{seen[op.time]!r} 和 {op.name!r}；"
                    "本规划器要求每个时刻至多一个 op，以便精确定义最后使用点"
                )
            seen[op.time] = op.name

    def _register_ops(
        self,
        ops: Sequence[Op],
        shapes: Dict[str, Tuple[int, ...]],
        producer: Dict[str, Op],
        consumers: Dict[str, List[Op]],
    ) -> None:
        """填充 shapes/producer/consumers，并校验时刻、重名与边的方向。"""
        for op in ops:
            if op.time < 1:
                raise DAGValidationError(
                    f"op {op.name!r} 的 time={op.time} 必须 >= 1"
                )
            if op.name in shapes:
                raise DAGValidationError(f"值名重复: {op.name!r}")
            for inp in op.inputs:
                if inp not in self._inputs:
                    prod = next(o for o in self._ops if o.name == inp)
                    if prod.time >= op.time:
                        raise DAGValidationError(
                            f"op {op.name!r}(time={op.time}) 引用了 "
                            f"{inp!r}(time={prod.time})，边必须从早指向晚"
                        )
            shapes[op.name] = op.shape
            producer[op.name] = op
            for inp in op.inputs:
                consumers[inp].append(op)

    def _build_slice_groups(
        self,
        ops: Sequence[Op],
        shapes: Mapping[str, Tuple[int, ...]],
    ) -> Tuple[Dict[str, List[Op]], Dict[str, str]]:
        """校验单级别名与完整划分，返回 (基底->切片列表, 值->存储组根)。"""
        slice_outputs = {op.name for op in ops if op.is_slice}
        for op in ops:
            if op.is_slice and op.base in slice_outputs:
                raise DAGValidationError(
                    f"slice {op.name!r} 的源 {op.base!r} 本身是切片视图；"
                    "本规划器只支持对基底张量直接切片（单级别名）"
                )

        slices: Dict[str, List[Op]] = {}
        root_of = {name: name for name in self._defined}
        for op in ops:
            if op.is_slice:
                slices.setdefault(op.base, []).append(op)
                root_of[op.name] = op.base
        for base, view_ops in slices.items():
            _validate_partition(base, shapes[base], view_ops)
        return slices, root_of

    def build(self) -> DAG:
        ops = sorted(self._ops, key=lambda op: (op.time, op.name))
        self._check_unique_times(ops)

        shapes: Dict[str, Tuple[int, ...]] = dict(self._inputs)
        producer: Dict[str, Op] = {}
        consumers: Dict[str, List[Op]] = {n: [] for n in self._defined}
        self._register_ops(ops, shapes, producer, consumers)

        # 单级别名检查在划分校验之前，否则嵌套切片会被误报为“划分不完整”
        slices, root_of = self._build_slice_groups(ops, shapes)

        return DAG(
            inputs=dict(self._inputs),
            ops=ops,
            shapes=shapes,
            producer=producer,
            consumers=consumers,
            slices=slices,
            root_of=root_of,
        )

    # ---- 内部辅助 ----
    def _check_name(self, name: str) -> None:
        if not isinstance(name, str) or not name:
            raise DAGValidationError(f"非法值名: {name!r}")
        if name in self._defined:
            raise DAGValidationError(f"值名重复: {name!r}")

    def _require_defined(self, *names: str) -> None:
        for name in names:
            if name not in self._defined:
                raise DAGValidationError(f"引用了未定义的值: {name!r}")

    def _shape(self, name: str) -> Tuple[int, ...]:
        if name in self._inputs:
            return self._inputs[name]
        return next(op.shape for op in self._ops if op.name == name)


def _check_shape(shape: Sequence[int]) -> None:
    if not shape:
        raise DAGValidationError("不支持标量（0 维）张量")
    for dim in shape:
        if dim <= 0:
            raise DAGValidationError(f"形状维度必须为正整数，得到 {tuple(shape)}")


def _validate_partition(
    base: str, base_shape: Tuple[int, ...], view_ops: Sequence[Op]
) -> None:
    """同一基底上的全部切片必须互不重叠，且并集恰好铺满基底。

    用覆盖计数网格实现：构造全 0 网格，把每个切片覆盖的区域 +1，
    最终要求处处恰好为 1（无重叠、无缝隙）。
    """
    cover = np.zeros(base_shape, dtype=np.int32)
    for op in view_ops:
        indexer = tuple(
            slice(start, start + size)
            for start, size in zip(op.starts, op.sizes)
        )
        region = cover[indexer]
        if np.any(region != 0):
            raise DAGValidationError(
                f"基底 {base!r} 上的切片 {op.name!r} 与其他切片重叠"
            )
        cover[indexer] = 1
    if not np.all(cover == 1):
        raise DAGValidationError(
            f"基底 {base!r} 的切片没有构成完整划分（存在未覆盖区域）"
        )
