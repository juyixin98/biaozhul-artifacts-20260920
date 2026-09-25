"""基于滑动稳健统计的因果脉冲（突发异常）检测器。

判决规则（对第 i 个样本）::

    z_i = |x_i - median(X_past)| / max(MAD(X_past)/0.6745, min_scale)
    anomaly_i = (z_i > threshold) 且预热完成 且 最近连续越限数 >= min_duration

其中 ``X_past`` 只包含严格早于 i 的、至多 ``window_size`` 个**有效**历史样本，
当前样本与未来样本永不参与，杜绝未来信息泄漏。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

import numpy as np

from .robust_stats import StreamingRobustStats


class Decision(str, Enum):
    """单点判决结果。"""

    WARMUP = "warmup"        # 有效历史不足 min_samples，不判异常
    MISSING = "missing"      # 缺样（NaN/inf），不参与统计、不计误报
    NORMAL = "normal"
    ANOMALY = "anomaly"


class MissingPolicy(str, Enum):
    """缺样策略（对非有限值 NaN/inf/-inf）。

    - ``SKIP``：输出 missing，不加入历史窗口，窗口保持上一状态不变；
    - ``HOLD``：用上一有效观测值替代后再按正常流程判决，替代值进入窗口。
    """

    SKIP = "skip"
    HOLD = "hold"


@dataclass(frozen=True)
class DetectorConfig:
    window_size: int = 200          # 滑动历史窗口长度（仅过去样本）
    threshold: float = 6.0          # 稳健 z 分数阈值（|z| > threshold 判越限）
    min_samples: int = 30           # 预热所需有效历史样本数
    min_scale: float = 1e-9         # 尺度绝对下限，防止常量信号 MAD=0 阈值塌缩
    min_duration: int = 1           # 连续越限样本数达到该值才确认异常（去抖）
    missing_policy: MissingPolicy = MissingPolicy.SKIP

    def __post_init__(self) -> None:
        if self.window_size <= 0:
            raise ValueError("window_size 必须为正整数")
        if self.threshold <= 0:
            raise ValueError("threshold 必须为正数")
        if self.min_samples <= 0:
            raise ValueError("min_samples 必须为正整数")
        if self.min_samples > self.window_size:
            raise ValueError("min_samples 不能大于 window_size")
        if self.min_scale < 0:
            raise ValueError("min_scale 不能为负")
        if self.min_duration <= 0:
            raise ValueError("min_duration 必须为正整数")
        if not isinstance(self.missing_policy, MissingPolicy):
            # 允许字符串构造时传入字符串。
            object.__setattr__(
                self, "missing_policy", MissingPolicy(str(self.missing_policy))
            )


@dataclass(frozen=True)
class PointResult:
    """单样本判决输出（全部为标量，便于逐点/逐块对齐核对）。"""

    index: int
    value: float           # 实际参与判决的值（HOLD 时为填补值；缺样为 NaN）
    zscore: float          # 稳健 z 分数；warmup/missing 时为 NaN
    median: float          # 判决所用历史中位数
    scale: float           # 判决所用历史稳健尺度
    decision: Decision
    n_history: int         # 判决时有效历史样本数


@dataclass
class BurstAnomalyDetector:
    """流式因果检测器。

    典型用法::

        det = BurstAnomalyDetector(DetectorConfig(...))
        for x in stream:
            r = det.update(x)   # 先判决
            # r.decision / r.zscore ...
    """

    config: DetectorConfig = field(default_factory=DetectorConfig)
    _stats: StreamingRobustStats = field(init=False)
    _last_valid: float = field(init=False, default=np.nan)
    _run: int = field(init=False, default=0)  # 当前连续越限计数

    def __post_init__(self) -> None:
        self._stats = StreamingRobustStats(self.config.window_size)

    # ---- 内部状态（仅供测试/诊断）----------------------------------------
    @property
    def n_history(self) -> int:
        return self._stats.size

    def reset(self) -> None:
        self._stats = StreamingRobustStats(self.config.window_size)
        self._last_valid = np.nan
        self._run = 0

    # ---- 核心逐点更新 ----------------------------------------------------
    def update(self, x, index: int | None = None) -> PointResult:
        """处理一个样本：**先用历史统计判决，再把该样本加入历史**。

        加入历史的样本是“实际参与判决的有效值”：
        - SKIP 策略下缺样不入窗；
        - HOLD 策略下以填补值入窗（与判决值一致，保证可复现）。
        被判异常的样本**照常入窗**：窗口如实反映真实过程；异常是瞬态
        脉冲时对中位数/MAD 影响很小，这正是选择稳健统计量的原因。
        """
        cfg = self.config
        raw = float(x)
        idx = -1 if index is None else int(index)
        is_missing = not np.isfinite(raw)

        if is_missing:
            if cfg.missing_policy is MissingPolicy.HOLD and np.isfinite(self._last_valid):
                value = self._last_valid
                filled = True
            else:
                # SKIP，或 HOLD 但尚无历史有效值：缺样不改变任何状态。
                return PointResult(
                    index=idx,
                    value=np.nan,
                    zscore=np.nan,
                    median=self._stats.median(),
                    scale=self._stats.scale(cfg.min_scale),
                    decision=Decision.MISSING,
                    n_history=self._stats.size,
                )
        else:
            value = raw
            filled = False

        med = self._stats.median()
        scl = self._stats.scale(cfg.min_scale)
        n = self._stats.size

        if n < cfg.min_samples:
            decision = Decision.WARMUP
            z = np.nan
            self._run = 0
        else:
            z = abs(value - med) / scl
            if z > cfg.threshold:
                self._run += 1
            else:
                self._run = 0
            decision = (
                Decision.ANOMALY if self._run >= cfg.min_duration else Decision.NORMAL
            )

        # 判决完成后才更新历史（严格因果）。
        self._stats.add(value)
        if not is_missing:
            self._last_valid = raw
        elif filled:
            # HOLD：_last_valid 保持上一个有效值不变即可。
            pass

        return PointResult(
            index=idx,
            value=value,
            zscore=z,
            median=med,
            scale=scl,
            decision=decision,
            n_history=n,
        )
