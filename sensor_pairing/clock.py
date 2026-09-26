"""时钟偏移校正与估计(基于 NumPy)."""

from __future__ import annotations

import numpy as np

from sensor_pairing.models import SensorMessage


def apply_clock_offset(
    messages: list[SensorMessage], offset: float
) -> list[SensorMessage]:
    """返回时间戳减去 offset 的新消息列表(不修改原列表).

    offset 定义为该流时钟相对参考时钟的领先量(秒).
    """
    return [
        SensorMessage(msg_id=m.msg_id, timestamp=m.timestamp - offset, data=m.data)
        for m in messages
    ]


def estimate_clock_offset(
    times_a: np.ndarray | list[float],
    times_b: np.ndarray | list[float],
    max_diff: float | None = None,
) -> float:
    """用最近邻时间差的中位数估计 B 相对 A 的时钟偏移.

    对每条 A 时间戳找最近的 B 时间戳, 差值的中位数对
    丢帧与抖动稳健. 返回的 offset 满足 t_b - t_a ≈ offset,
    可直接作为 TimePairingMatcher 的 clock_offset_b.

    适用前提: 两路采样**同一事件序列**(相同触发, 仅时钟平移),
    且 |offset| 小于采样间隔的一半. 对各自独立周期采样、频率不同
    的两路流, 时间戳之间不存在逐条对应关系, 本方法失效, 请改用
    estimate_offset_from_data(利用观测数据对齐).

    Args:
        times_a: 流 A 时间戳数组.
        times_b: 流 B 时间戳数组.
        max_diff: 若给定, 仅统计 |差值| <= max_diff 的样本.

    Raises:
        ValueError: 任一输入为空, 或 max_diff 过滤后无样本.
    """
    a = np.asarray(times_a, dtype=np.float64)
    b = np.asarray(times_b, dtype=np.float64)
    if a.size == 0 or b.size == 0:
        raise ValueError("两路时间戳均不能为空")

    # 向量化最近邻: 对 b 排序后二分查找每个 a 的位置.
    order = np.argsort(b)
    b_sorted = b[order]
    idx = np.searchsorted(b_sorted, a)
    idx_right = np.clip(idx, 0, b_sorted.size - 1)
    idx_left = np.clip(idx - 1, 0, b_sorted.size - 1)
    diff_right = b_sorted[idx_right] - a
    diff_left = b_sorted[idx_left] - a
    diffs = np.where(np.abs(diff_left) <= np.abs(diff_right), diff_left, diff_right)

    if max_diff is not None:
        diffs = diffs[np.abs(diffs) <= max_diff]
        if diffs.size == 0:
            raise ValueError("max_diff 过滤后没有可用样本")

    return float(np.median(diffs))


def estimate_offset_from_data(
    times_a: np.ndarray | list[float],
    data_a: np.ndarray | list[list[float]],
    times_b: np.ndarray | list[float],
    data_b: np.ndarray | list[list[float]],
    search_range: float = 0.25,
    coarse_step: float = 0.005,
    min_samples: int = 10,
) -> float:
    """数据辅助的时钟偏移估计: 网格搜索使轨迹对齐误差最小的偏移.

    两路传感器观测同一运动轨迹时, 对每个候选偏移 c 将 A 的观测
    插值到 (t_b - c) 时刻, 与 B 的观测比较均方误差, 取最小者.
    可恢复大于采样周期的偏移(无 estimate_clock_offset 的混叠限制).
    先粗搜(coarse_step)再在最优点附近细化(步长缩小 10 倍).

    Args:
        times_a / data_a: 流 A 时间戳与观测向量(形状 (N, D)).
        times_b / data_b: 流 B 时间戳与观测向量(形状 (M, D)).
        search_range: 搜索范围 ±search_range(秒).
        coarse_step: 粗搜步长(秒).
        min_samples: 有效重叠样本数下限.

    Raises:
        ValueError: 输入形状不一致或重叠样本不足.
    """
    t_a = np.asarray(times_a, dtype=np.float64)
    d_a = np.asarray(data_a, dtype=np.float64)
    t_b = np.asarray(times_b, dtype=np.float64)
    d_b = np.asarray(data_b, dtype=np.float64)
    if t_a.ndim != 1 or t_b.ndim != 1 or d_a.ndim != 2 or d_b.ndim != 2:
        raise ValueError("时间戳须为一维, 观测须为二维 (N, D)")
    if d_a.shape[0] != t_a.size or d_b.shape[0] != t_b.size:
        raise ValueError("时间戳与观测数量不一致")
    if d_a.shape[1] != d_b.shape[1]:
        raise ValueError("两路观测维度不一致")
    order = np.argsort(t_a)
    t_a, d_a = t_a[order], d_a[order]

    def alignment_error(offset: float) -> float:
        tq = t_b - offset
        mask = (tq >= t_a[0]) & (tq <= t_a[-1])
        if int(mask.sum()) < min_samples:
            return np.inf
        interp = np.column_stack(
            [np.interp(tq[mask], t_a, d_a[:, j]) for j in range(d_a.shape[1])]
        )
        return float(np.mean((interp - d_b[mask]) ** 2))

    coarse = np.arange(-search_range, search_range + coarse_step, coarse_step)
    errors = np.array([alignment_error(float(c)) for c in coarse])
    if not np.isfinite(errors).any():
        raise ValueError("搜索范围内重叠样本不足")
    best = float(coarse[int(np.argmin(errors))])

    fine_step = coarse_step / 10.0
    fine = np.arange(best - coarse_step, best + coarse_step + fine_step, fine_step)
    errors = np.array([alignment_error(float(c)) for c in fine])
    return float(fine[int(np.argmin(errors))])
