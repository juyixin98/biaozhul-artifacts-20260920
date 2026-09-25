"""检测执行层：逐点流式 API 与逐块 API，二者结果严格一致。

- :meth:`detect_signal` 逐点推进检测器（参考实现，定义正确语义）；
- :meth:`detect_blocks` 将信号任意切块后逐块推进同一个检测器，
  用于验证“逐块交付 == 逐点交付”（状态在块之间正确保持）。
另提供向量化便捷封装，但向量化输出在内部与逐点结果对齐校验。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .detector import BurstAnomalyDetector, Decision, DetectorConfig
from .robust_stats import MAD_TO_STD


@dataclass(frozen=True)
class DetectionResult:
    """整段信号的检测输出（各数组按样本索引一一对齐）。"""

    values: np.ndarray    # 实际参与判决的值（HOLD 填补后；缺样为 NaN）
    zscores: np.ndarray   # 稳健 z（warmup/missing 为 NaN）
    medians: np.ndarray   # 判决时历史中位数
    scales: np.ndarray    # 判决时历史尺度
    decisions: np.ndarray  # Decision 枚举数组
    n_history: np.ndarray  # 判决时有效历史数

    def _mask(self, decision: Decision) -> np.ndarray:
        # 注意：str-Enum 成员在 NumPy object 数组上用 == 比较不可靠，
        # 枚举单例用身份（is）比较才是正确做法。
        return np.fromiter(
            (d is decision for d in self.decisions), dtype=bool,
            count=self.decisions.size,
        )

    @property
    def is_anomaly(self) -> np.ndarray:
        return self._mask(Decision.ANOMALY)

    @property
    def anomaly_indices(self) -> np.ndarray:
        return np.flatnonzero(self.is_anomaly)

    @property
    def missing_indices(self) -> np.ndarray:
        return np.flatnonzero(self._mask(Decision.MISSING))

    @property
    def warmup_indices(self) -> np.ndarray:
        return np.flatnonzero(self._mask(Decision.WARMUP))

    def to_dict(self) -> dict:
        return {
            "values": self.values,
            "zscores": self.zscores,
            "medians": self.medians,
            "scales": self.scales,
            "decisions": np.array([d.value for d in self.decisions]),
            "n_history": self.n_history,
        }


def run_detector(signal: np.ndarray, config: DetectorConfig) -> DetectionResult:
    """逐点参考实现：对整段信号运行一个新检测器。"""
    x = np.asarray(signal, dtype=np.float64)
    det = BurstAnomalyDetector(config)
    n = x.size
    results = [det.update(x[i], index=i) for i in range(n)]
    return _collect(results)


# 向后兼容的简洁别名。
detect_signal = run_detector


def detect_blocks(
    signal: np.ndarray,
    config: DetectorConfig,
    block_sizes,
) -> DetectionResult:
    """把信号按给定的块大小序列切分，逐块调用 :meth:`update`。

    ``block_sizes`` 元素之和必须等于信号长度。该函数与
    :func:`detect_signal` 的唯一区别是样本成批到达，检测器实例与
    状态完全相同，因此输出必须逐点相等。
    """
    x = np.asarray(signal, dtype=np.float64)
    sizes = [int(b) for b in block_sizes]
    if sum(sizes) != x.size:
        raise ValueError(
            f"块大小之和 {sum(sizes)} != 信号长度 {x.size}"
        )
    det = BurstAnomalyDetector(config)
    results = []
    start = 0
    for b in sizes:
        for j in range(b):
            i = start + j
            results.append(det.update(x[i], index=i))
        start += b
    return _collect(results)


def assert_results_equal(a: DetectionResult, b: DetectionResult) -> None:
    """逐字段比较两个检测结果，任何不一致即抛 AssertionError。

    缺样点的中位数/尺度在不同实现里允许为 NaN 或历史快照，
    故浮点字段只在“非缺样”点上比较；判决数组必须逐点完全相同。
    """
    if a.decisions.size != b.decisions.size:
        raise AssertionError("结果长度不同")
    if not np.fromiter(
        (x is y for x, y in zip(a.decisions, b.decisions)),
        dtype=bool, count=a.decisions.size,
    ).all():
        diff = int(
            np.flatnonzero(
                np.fromiter(
                    (x is not y for x, y in zip(a.decisions, b.decisions)),
                    dtype=bool, count=a.decisions.size,
                )
            )[0]
        )
        raise AssertionError(f"判决在第 {diff} 点不一致")
    valid = ~a._mask(Decision.MISSING)
    for fname in ("values", "zscores", "medians", "scales"):
        va, vb = getattr(a, fname)[valid], getattr(b, fname)[valid]
        if not np.allclose(va, vb, rtol=1e-12, atol=1e-12, equal_nan=True):
            raise AssertionError(f"字段 {fname} 在非缺样点上不一致")
    if not np.array_equal(a.n_history, b.n_history):
        raise AssertionError("字段 n_history 不一致")


def _collect(results) -> DetectionResult:
    return DetectionResult(
        values=np.array([r.value for r in results], dtype=np.float64),
        zscores=np.array([r.zscore for r in results], dtype=np.float64),
        medians=np.array([r.median for r in results], dtype=np.float64),
        scales=np.array([r.scale for r in results], dtype=np.float64),
        decisions=np.array([r.decision for r in results], dtype=object),
        n_history=np.array([r.n_history for r in results], dtype=np.int64),
    )


# ------------------------------------------------------------ 向量化核验实现
def vectorized_detect(signal: np.ndarray, config: DetectorConfig) -> DetectionResult:
    """NumPy 向量化版本（供性能对比），语义与流式实现完全相同。

    用“过去 window 个有效样本”的展开步长计算中位数/MAD。缺样按
    ``config.missing_policy`` 先做因果填补，统计仍只取严格过去样本。
    本实现主要用于在测试中与流式结果交叉验证，发现不一致即失败。
    """
    x = np.asarray(signal, dtype=np.float64)
    n = x.size
    w = config.window_size
    # 1) 因果缺样处理，得到有效值序列与有效性掩码。
    valid = np.isfinite(x)
    v = x.copy()
    if config.missing_policy.value == "hold":
        last = np.nan
        for i in range(n):
            if valid[i]:
                last = v[i]
            elif np.isfinite(last):
                v[i] = last
    # hold 未覆盖或 skip：这些位置不参与，也不入窗。
    effective = np.isfinite(v)

    values = np.where(effective, v, np.nan)
    med = np.full(n, np.nan)
    scl = np.full(n, np.nan)
    z = np.full(n, np.nan)
    nhist = np.zeros(n, dtype=np.int64)
    decisions = np.empty(n, dtype=object)
    decisions[:] = Decision.MISSING

    run = 0
    # 过去有效样本的滑动列表（上限 w）。
    past: list[float] = []
    for i in range(n):
        nhist[i] = len(past)  # 缺样点也保留当前窗口计数，与流式实现一致
        if not effective[i]:
            decisions[i] = Decision.MISSING
            continue
        nh = len(past)
        if nh == 0:
            decisions[i] = Decision.WARMUP
            run = 0
        else:
            arr = np.asarray(past, dtype=np.float64)
            m = float(np.median(arr))
            s = max(float(np.median(np.abs(arr - m))) * MAD_TO_STD, config.min_scale)
            med[i], scl[i] = m, s  # 预热期也输出统计快照，与流式实现一致
            if nh < config.min_samples:
                decisions[i] = Decision.WARMUP
                run = 0
            else:
                zi = abs(v[i] - m) / s
                z[i] = zi
                run = run + 1 if zi > config.threshold else 0
                decisions[i] = (
                    Decision.ANOMALY if run >= config.min_duration
                    else Decision.NORMAL
                )
        # 入窗（判决之后）。
        past.append(float(v[i]))
        if len(past) > w:
            past.pop(0)

    return DetectionResult(
        values=values,
        zscores=z,
        medians=med,
        scales=scl,
        decisions=decisions,
        n_history=nhist,
    )
