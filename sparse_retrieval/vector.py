"""稀疏向量的核心数据结构与语义约定。

设计要点
--------
1. 稀疏向量用 ``(indices, values)`` 两个等长列表表示。
2. **重复维度先合并**: 构造时对同一维度的权重求和, 合并后若权重变为
   0.0 则该维度被丢弃(例如 +1.0 与 -1.0 互相抵消)。
3. **零向量语义明确**: 不含任何非零维度的向量是合法向量,
   余弦相似度在涉及零向量时没有数学定义, 统一定义为 0.0
   (既不是 +1 也不是 NaN), 由 :mod:`sparse_retrieval.scoring` 落实。
4. 合并后输出按维度编号升序、去重, 保证下游倒排链与点积计算确定。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

__all__ = [
    "SparseVectorError",
    "SparseVector",
    "l2_norm",
    "merge_entries",
    "normalize_entries",
    "parse_entries",
]


class SparseVectorError(ValueError):
    """稀疏向量输入非法(维度编号越界、非有限数值等)。"""


@dataclass(frozen=True)
class SparseVector:
    """不可变稀疏向量。

    Attributes:
        indices: 升序、去重后的维度编号(int64)。
        values:  与 ``indices`` 对齐的非零权重(float64)。
        dim:     向量空间维度(下标合法范围为 ``[0, dim)``)。
    """

    indices: np.ndarray
    values: np.ndarray
    dim: int

    def __post_init__(self) -> None:
        idx = np.asarray(self.indices, dtype=np.int64)
        val = np.asarray(self.values, dtype=np.float64)
        if idx.ndim != 1 or val.ndim != 1 or idx.shape != val.shape:
            raise SparseVectorError("indices 与 values 必须是等长一维数组")
        if not isinstance(self.dim, int) or isinstance(self.dim, bool) or self.dim <= 0:
            raise SparseVectorError("dim 必须是正整数")
        # frozen dataclass: 通过 object.__setattr__ 写入规范化结果
        object.__setattr__(self, "indices", idx)
        object.__setattr__(self, "values", val)

    @classmethod
    def create(
        cls,
        indices: np.ndarray | list[int],
        values: np.ndarray | list[float],
        dim: int,
    ) -> "SparseVector":
        """构造向量: 校验 -> 合并重复维度 -> 删除抵消为 0 的维度。"""
        merged_idx, merged_val = merge_entries(indices, values, dim)
        return cls(merged_idx, merged_val, dim)

    @property
    def nnz(self) -> int:
        """非零维度个数。"""
        return int(self.indices.shape[0])

    @property
    def norm(self) -> float:
        """L2 范数; 零向量为 0.0。"""
        return float(np.sqrt(np.dot(self.values, self.values))) if self.nnz else 0.0

    def to_dict(self) -> dict:
        return {
            "dim": self.dim,
            "indices": self.indices.tolist(),
            "values": self.values.tolist(),
        }


def merge_entries(
    indices: np.ndarray | list[int],
    values: np.ndarray | list[float],
    dim: int,
) -> tuple[np.ndarray, np.ndarray]:
    """校验并合并重复维度, 返回升序去重的 ``(indices, values)``。

    同一维度出现多次时权重**求和**(支持负权重互相抵消)。
    求和结果恰为 0 的维度从结果中删除, 但 ``-0.0`` 与 +0 视为零。
    遇到 NaN / +-inf 直接报错, 避免污染排序与分数。
    """
    idx = np.asarray(indices, dtype=np.int64)
    val = np.asarray(values, dtype=np.float64)
    if idx.ndim != 1 or val.ndim != 1 or idx.shape != val.shape:
        raise SparseVectorError("indices 与 values 必须是等长一维数组")
    if not isinstance(dim, int) or isinstance(dim, bool) or dim <= 0:
        raise SparseVectorError("dim 必须是正整数")
    if idx.size and (int(idx.min()) < 0 or int(idx.max()) >= dim):
        raise SparseVectorError(f"维度编号必须落在 [0, {dim}) 内")
    if not np.all(np.isfinite(val)):
        raise SparseVectorError("权重必须是有限实数(不接受 NaN/Inf)")

    if idx.size == 0:
        return idx.copy(), val.copy()

    order = np.argsort(idx, kind="mergesort")
    idx_sorted = idx[order]
    val_sorted = val[order]

    unique_pos = np.flatnonzero(np.r_[True, idx_sorted[1:] != idx_sorted[:-1]])
    merged = np.add.reduceat(val_sorted, unique_pos)
    merged_idx = idx_sorted[unique_pos]

    nonzero = merged != 0.0
    return merged_idx[nonzero], merged[nonzero]


def l2_norm(values: np.ndarray) -> float:
    """计算权重数组的 L2 范数。"""
    if values.shape[0] == 0:
        return 0.0
    return float(np.sqrt(np.dot(values, values)))


def normalize_entries(
    indices: np.ndarray, values: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """L2 归一化(零向量原样返回, 不做除法)。"""
    norm = l2_norm(values)
    if norm == 0.0:
        return indices.copy(), values.copy()
    return indices.copy(), values / norm


def parse_entries(
    payload: dict,
    *,
    default_dim: int | None = None,
    require_dim: bool = False,
) -> tuple[list[int], list[float], int]:
    """解析 API 请求中的向量 JSON。

    支持两种形态::

        {"dim": 1000, "entries": [{"index": 3, "value": -1.5}, ...]}
        {"dim": 1000, "indices": [3, 7], "values": [-1.5, 2.0]}

    空 ``entries`` 表示零向量。``require_dim`` 用于建库请求必须显式
    声明维度; 查询请求缺省时回退到索引维度(``default_dim``)。
    """
    if not isinstance(payload, dict):
        raise SparseVectorError("请求体必须是 JSON 对象")

    dim = payload.get("dim", default_dim)
    if require_dim and dim is None:
        raise SparseVectorError("缺少必填字段 dim")
    if dim is not None:
        if not isinstance(dim, int) or isinstance(dim, bool) or dim <= 0:
            raise SparseVectorError("dim 必须是正整数")

    if "entries" in payload:
        raw = payload["entries"]
        if not isinstance(raw, list):
            raise SparseVectorError("entries 必须是数组")
        indices: list[int] = []
        values: list[float] = []
        for i, item in enumerate(raw):
            if not isinstance(item, dict) or "index" not in item or "value" not in item:
                raise SparseVectorError(f"entries[{i}] 必须包含 index 与 value")
            index = item["index"]
            value = item["value"]
            if not isinstance(index, int) or isinstance(index, bool):
                raise SparseVectorError(f"entries[{i}].index 必须是整数")
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise SparseVectorError(f"entries[{i}].value 必须是数值")
            indices.append(index)
            values.append(float(value))
    elif "indices" in payload and "values" in payload:
        indices_raw, values_raw = payload["indices"], payload["values"]
        if not isinstance(indices_raw, list) or not isinstance(values_raw, list):
            raise SparseVectorError("indices 与 values 必须是数组")
        if len(indices_raw) != len(values_raw):
            raise SparseVectorError("indices 与 values 长度必须一致")
        indices, values = [], []
        for i, (index, value) in enumerate(zip(indices_raw, values_raw)):
            if not isinstance(index, int) or isinstance(index, bool):
                raise SparseVectorError(f"indices[{i}] 必须是整数")
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise SparseVectorError(f"values[{i}] 必须是数值")
            indices.append(index)
            values.append(float(value))
    else:
        raise SparseVectorError("向量数据必须通过 entries 或 indices/values 提供")

    return indices, values, int(dim) if dim is not None else None  # type: ignore[return-value]
