"""桶归约核心。

职责（且仅有这一个职责）：给定一个桶的固定布局与若干工作者的提交，
按**逐槽位遮罩 + 真实样本数加权**计算平均。

关键语义（直接对应四条边界要求）：

1. **槽位固定**：桶向量长度由 :class:`~gradbucket.tensors.BucketLayout` 决定，
   每个参数占定长槽位。提交长度不符一律拒绝，绝不静默截断或补零。
2. **遮罩逐槽成立或整体缺失**：一个参数要么整槽计算了（mask 全 True），
   要么整槽是占位（mask 全 False）。半截遮罩是非法提交（REJECT 类别），
   防止"部分元素被当零平均"。
3. **按真实样本数加权**：槽位结果 = Σ(n_w·g_w) / Σ n_w，分母只统计
   **覆盖该槽位**的工作者。不同槽位的分母可以不同（缺梯度逐槽剔除）。
4. **零证据槽不猜**：若某槽位没有任何覆盖者，输出零但标记
   ``covered=False``，由上层决定无法判定，而不是把零当成"平均梯度=0"。

本模块**不持有训练状态、不做权重更新、不发网络请求**，因此可以独立、
穷尽地做数值测试。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .diagnostics import tensor_fingerprint
from .tensors import BucketLayout, Slot


class ReductionError(ValueError):
    """归约拒绝基类，``code`` 为稳定的失败类别字符串。"""

    code = "REDUCTION_ERROR"


class BucketShapeError(ReductionError):
    code = "REJECTED_BUCKET_SHAPE"


class PartialSlotMaskError(ReductionError):
    code = "REJECTED_PARTIAL_SLOT_MASK"


class SampleCountError(ReductionError):
    code = "REJECTED_SAMPLE_COUNT"


class DuplicateContributionError(ReductionError):
    code = "REJECTED_DUPLICATE"


class NonFiniteGradientError(ReductionError):
    code = "REJECTED_NON_FINITE"


# float64 能精确表示的最大整数；超过它样本数在加权分母中会静默丢精度。
MAX_SAMPLE_COUNT = 2 ** 53


@dataclass(frozen=True)
class Contribution:
    """单个工作者对单个桶的一次提交。"""

    worker_id: str
    vec: np.ndarray          # 一维，长度 = 该桶槽位总长
    mask: np.ndarray         # 布尔，一维同长；True=该元素是真实梯度
    sample_count: int
    request_id: str


@dataclass(frozen=True)
class SlotReport:
    """单槽归约依据：谁参与、分母多大、结果是否有证据支撑。"""

    slot_index: int
    param: str
    shape: tuple[int, ...]
    covered: bool
    contributor_ids: tuple[str, ...]
    weight_total: float           # Σ 真实样本数（仅覆盖者）
    contributor_counts: tuple[int, ...]
    result_fingerprint: dict

    def to_dict(self) -> dict:
        return {
            "slot": self.slot_index,
            "param": self.param,
            "shape": list(self.shape),
            "covered": bool(self.covered),
            "contributors": list(self.contributor_ids),
            "sample_counts": [int(c) for c in self.contributor_counts],
            "weight_total": float(self.weight_total),
            "result": self.result_fingerprint,
        }


@dataclass(frozen=True)
class BucketReduction:
    bucket_index: int
    vec: np.ndarray               # 归约后的一维桶向量
    slots: tuple[SlotReport, ...]

    @property
    def all_slots_covered(self) -> bool:
        return all(s.covered for s in self.slots)

    def basis(self) -> dict:
        """归约依据（供诊断输出加权分母与参与方）。"""
        return {
            "bucket": self.bucket_index,
            "all_slots_covered": self.all_slots_covered,
            "slots": [s.to_dict() for s in self.slots],
        }


def _check_sample_count(n, worker_id: str) -> int:
    """样本数必须是真正的正整数（bool 不算）且不超过精确可表示上界。"""
    if isinstance(n, bool) or not isinstance(n, (int, np.integer)):
        raise SampleCountError(
            f"工作者 {worker_id} 样本数非法: {n!r}（必须为整数，不能是布尔）"
        )
    n = int(n)
    if n <= 0:
        raise SampleCountError(
            f"工作者 {worker_id} 样本数非法: {n}（必须为正整数）"
        )
    if n > MAX_SAMPLE_COUNT:
        raise SampleCountError(
            f"工作者 {worker_id} 样本数 {n} 超过精确上界 2^53，"
            "会在 float64 加权分母中溢出/丢精度"
        )
    return n


def _validate_contribution(c: Contribution, expected_len: int) -> None:
    vec = np.asarray(c.vec, dtype=np.float64)
    mask = np.asarray(c.mask, dtype=bool)
    if vec.ndim != 1 or vec.shape[0] != expected_len:
        raise BucketShapeError(
            f"工作者 {c.worker_id} 桶向量长度 {vec.shape} 与布局 {expected_len} 不符"
        )
    if mask.shape != vec.shape:
        raise BucketShapeError(
            f"工作者 {c.worker_id} mask 形状 {mask.shape} 与向量 {vec.shape} 不符"
        )
    _check_sample_count(c.sample_count, c.worker_id)
    # 仅校验真实梯度区（mask=True）：占位区不进入归约，其数值无关紧要。
    if mask.any() and not np.all(np.isfinite(vec[mask])):
        raise NonFiniteGradientError(
            f"工作者 {c.worker_id} 真实梯度含非有限值（inf/NaN），拒绝污染归约"
        )


def validate_contribution_for_slots(
    c: Contribution, bounds: list[tuple[int, int]]
) -> None:
    """长度/形状/样本数 + 整槽遮罩 + 非有限的统一权威校验。

    reducer 与 coordinator 共用这一份，避免规则两处手写漂移。
    ``bounds`` 为各槽在桶向量中的局部 [lo, hi)。
    """
    expected_len = sum(hi - lo for lo, hi in bounds)
    _validate_contribution(c, expected_len)
    mask = np.asarray(c.mask, dtype=bool)
    for (lo, hi) in bounds:
        piece = mask[lo:hi]
        if not (piece.all() or not piece.any()):
            raise PartialSlotMaskError(
                f"工作者 {c.worker_id} 参数槽位 [{lo}:{hi}] 出现半截遮罩，"
                "参数必须整体计算或整体占位"
            )


def _local_bounds(slots: tuple[Slot, ...]) -> list[tuple[int, int]]:
    """槽位在*桶向量*中的局部 [lo, hi)——桶向量总是从 0 开始。"""
    bounds = []
    cursor = 0
    for s in slots:
        bounds.append((cursor, cursor + s.length))
        cursor += s.length
    return bounds


def reduce_bucket(
    layout: BucketLayout,
    bucket_index: int,
    contributions: list[Contribution],
) -> BucketReduction:
    """执行单桶加权归约。纯函数：输入相同输出相同，与到达顺序无关。

    内部按 worker_id 排序后累加，保证浮点结合顺序固定；测试会以不同的
    提交顺序调用本函数，断言逐位一致。
    """
    slots = layout.slots_for_bucket(bucket_index)
    expected_len = sum(s.length for s in slots)
    bounds = _local_bounds(slots)

    seen: set[str] = set()
    for c in contributions:
        if c.worker_id in seen:
            raise DuplicateContributionError(
                f"工作者 {c.worker_id} 对同一桶重复提交"
            )
        seen.add(c.worker_id)
        validate_contribution_for_slots(c, bounds)

    ordered = sorted(contributions, key=lambda c: c.worker_id)
    accum = np.zeros(expected_len, dtype=np.float64)

    reports: list[SlotReport] = []
    for s, (lo, hi) in zip(slots, bounds):
        contributors: list[str] = []
        counts: list[int] = []
        weight_total = 0  # 标量分母：同槽各元素共享同一个有效样本总数
        # 溢出在随后的有限性检查中显式处置，这里抑制预期内的 RuntimeWarning。
        with np.errstate(over="ignore"):
            for c in ordered:
                if bool(np.asarray(c.mask)[lo]):  # 整槽遮罩，取首元素即可
                    n = int(c.sample_count)
                    accum[lo:hi] += n * np.asarray(c.vec, dtype=np.float64)[lo:hi]
                    weight_total += n
                    contributors.append(c.worker_id)
                    counts.append(n)
            covered = weight_total > 0
            if covered:
                result = accum[lo:hi] / weight_total
            else:
                result = np.zeros(hi - lo)
        # 输入合法不代表加权结果有限；非有限结果必须拦下，不得产生 NaN 权重。
        if covered and not np.all(np.isfinite(result)):
            raise NonFiniteGradientError(
                f"桶 {bucket_index} 参数 {s.param.name!r} 归约结果含"
                "inf/NaN（中间累加可能溢出），拒绝该桶"
            )
        reports.append(
            SlotReport(
                slot_index=s.index,
                param=s.param.name,
                shape=s.param.shape,
                covered=covered,
                contributor_ids=tuple(contributors),
                weight_total=float(weight_total),
                contributor_counts=tuple(counts),
                result_fingerprint=tensor_fingerprint(result),
            )
        )
        accum[lo:hi] = result

    return BucketReduction(bucket_index=bucket_index, vec=accum, slots=tuple(reports))
