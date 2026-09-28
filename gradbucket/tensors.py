"""张量类型模块。

职责：
- 描述参数张量（名字、形状、dtype），不承担任何训练逻辑。
- 在一轮开始时由固定的参数顺序生成**固定桶布局**（:class:`BucketLayout`）。
- 每个参数在桶向量中占一个**定长槽位**（slot）；轮内槽位顺序与偏移永不重排，
  因此"计算完成顺序不同"不会改变归约结果。
- 未使用/未计算的参数必须由提交方显式填充占位（零向量 + mask=False），
  不允许缺槽——这是"未用参数有显式占位"的类型层保证。

这里**没有**任何归约或训练状态，纯数据结构 + 打包/解包。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

import numpy as np

SUPPORTED_DTYPE = "float64"


def _canonical_dtype(dtype: str) -> str:
    alias = {"double": "float64", "float": "float32"}
    name = alias.get(dtype, dtype)
    if name != SUPPORTED_DTYPE:
        raise ValueError(f"仅支持 {SUPPORTED_DTYPE}，收到 dtype={dtype!r}")
    return name


@dataclass(frozen=True)
class ParamSpec:
    """参数描述：名字 + 形状。轮内身份的唯一依据。"""

    name: str
    shape: tuple[int, ...]
    dtype: str = SUPPORTED_DTYPE

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("参数名不能为空")
        if any(d <= 0 for d in self.shape):
            raise ValueError(f"参数 {self.name!r} 形状非法: {self.shape}")
        # frozen dataclass 上规范化 dtype
        object.__setattr__(self, "dtype", _canonical_dtype(self.dtype))

    @property
    def size(self) -> int:
        return int(np.prod(self.shape))

    def zeros(self) -> np.ndarray:
        return np.zeros(self.shape, dtype=np.float64)


@dataclass(frozen=True)
class Slot:
    """桶向量内的一个定长槽位。"""

    index: int
    param: ParamSpec
    offset: int
    length: int


@dataclass(frozen=True)
class BucketLayout:
    """一轮的固定桶布局。

    ``slots`` 严格按构造时的参数顺序排列；``bucket_boundaries`` 把槽位序列
    切成若干桶。布局对象不可变，归约只能读它。
    """

    round_index: int
    slots: tuple[Slot, ...]
    bucket_sizes: tuple[int, ...]

    def __post_init__(self) -> None:
        if not self.slots:
            raise ValueError("布局至少需要一个参数槽位")
        if sum(self.bucket_sizes) != len(self.slots):
            raise ValueError("桶大小之和必须等于槽位总数")

    @property
    def num_buckets(self) -> int:
        return len(self.bucket_sizes)

    @property
    def total_length(self) -> int:
        return self.slots[-1].offset + self.slots[-1].length

    def slots_for_bucket(self, bucket_index: int) -> tuple[Slot, ...]:
        start = sum(self.bucket_sizes[:bucket_index])
        return self.slots[start : start + self.bucket_sizes[bucket_index]]

    def slot_by_name(self, name: str) -> Slot:
        for s in self.slots:
            if s.param.name == name:
                return s
        raise KeyError(name)

    def describe(self) -> list[dict]:
        """供诊断/接口输出的无张量布局描述。"""
        return [
            {
                "slot": s.index,
                "param": s.param.name,
                "shape": list(s.param.shape),
                "offset": s.offset,
                "length": s.length,
            }
            for s in self.slots
        ]


def build_layout(
    round_index: int,
    params: Sequence[ParamSpec],
    bucket_capacity: int,
) -> BucketLayout:
    """按参数顺序顺序填满容量为 ``bucket_capacity`` 的桶。

    同名参数重复直接报错——轮内参数身份必须唯一。布局只依赖参数声明顺序，
    与梯度何时到达无关。
    """
    if bucket_capacity <= 0:
        raise ValueError("bucket_capacity 必须为正")
    names: set[str] = set()
    slots: list[Slot] = []
    offset = 0
    for i, p in enumerate(params):
        if p.name in names:
            raise ValueError(f"参数名重复: {p.name!r}")
        names.add(p.name)
        slots.append(Slot(index=i, param=p, offset=offset, length=p.size))
        offset += p.size
    n = len(slots)
    buckets = tuple(
        min(bucket_capacity, n - start)
        for start in range(0, n, bucket_capacity)
    )
    return BucketLayout(
        round_index=round_index,
        slots=tuple(slots),
        bucket_sizes=buckets,
    )


# ---------------------------------------------------------------------------
# 打包 / 解包：参数 dict <-> 定长桶向量
# ---------------------------------------------------------------------------


def pack_bucket(
    layout: BucketLayout,
    bucket_index: int,
    values: dict[str, np.ndarray],
    *,
    allow_missing: bool = False,
) -> np.ndarray:
    """把该桶各参数按槽位顺序拷进一维向量。

    ``allow_missing=False``（默认）时缺参数直接抛错——提交方必须先为未计算
    的参数放入显式占位。向量长度严格等于该桶所有槽位长度之和。
    """
    slots = layout.slots_for_bucket(bucket_index)
    total = sum(s.length for s in slots)
    vec = np.zeros(total, dtype=np.float64)
    cursor = 0
    for s in slots:
        if s.param.name not in values:
            if allow_missing:
                cursor += s.length
                continue
            raise KeyError(f"桶 {bucket_index} 缺少参数 {s.param.name!r} 的槽位数据")
        arr = np.asarray(values[s.param.name], dtype=np.float64)
        if arr.shape != s.param.shape:
            raise ValueError(
                f"参数 {s.param.name!r} 形状不匹配: 期望 {s.param.shape}, 实际 {arr.shape}"
            )
        vec[cursor : cursor + s.length] = arr.reshape(-1)
        cursor += s.length
    return vec


def unpack_bucket(
    layout: BucketLayout,
    bucket_index: int,
    vec: np.ndarray,
) -> dict[str, np.ndarray]:
    """把归约后的一维桶向量还原成按参数名索引的数组 dict。"""
    slots = layout.slots_for_bucket(bucket_index)
    expected = sum(s.length for s in slots)
    flat = np.asarray(vec, dtype=np.float64).reshape(-1)
    if flat.shape[0] != expected:
        raise ValueError(f"桶 {bucket_index} 向量长度 {flat.shape[0]} != 布局 {expected}")
    out: dict[str, np.ndarray] = {}
    cursor = 0
    for s in slots:
        out[s.param.name] = flat[cursor : cursor + s.length].reshape(s.param.shape).copy()
        cursor += s.length
    return out


def placeholder_for(slot: Slot) -> np.ndarray:
    """未使用参数的显式占位：形状正确的零向量。"""
    return slot.param.zeros()


def empty_payload(
    layout: BucketLayout,
    bucket_index: int,
    present: Iterable[str] = (),
) -> tuple[np.ndarray, np.ndarray]:
    """生成 (全零桶向量, 全 False mask)，再把 present 中的槽位置 True。

    供测试与工作者构造"缺梯度但槽位完整"的提交。
    """
    slots = layout.slots_for_bucket(bucket_index)
    total = sum(s.length for s in slots)
    vec = np.zeros(total, dtype=np.float64)
    mask = np.zeros(total, dtype=bool)
    present_set = set(present)
    cursor = 0
    for s in slots:
        if s.param.name in present_set:
            mask[cursor : cursor + s.length] = True
        cursor += s.length
    return vec, mask
