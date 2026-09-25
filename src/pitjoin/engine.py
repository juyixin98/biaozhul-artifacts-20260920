"""离线特征时间点连接（point-in-time join）引擎。

核心规则
========
对事件 ``(entity_id, event_time)`` 与某特征，选中的记录必须同时满足：

1. ``effective_time <= event_time`` —— 业务生效时间不晚于事件时刻（as-of）；
2. ``ingest_time   <= as_of``       —— 记录在"提问时点"之前已经入库可见。

离线回放时 ``as_of = event_time``：我们假装站在事件发生的那一刻提问，
任何事后才入库的修订（即便其业务生效时间很早）都不可见，从而杜绝
事后修订泄漏（look-ahead / revision leakage）。

同一生效时刻存在多个版本时的确定性优先级：
``max(effective_time)`` → ``max(ingest_time)`` → ``max(version)`` →
仍完全相同则视为重复记录，稳定取首条并在依据中标记 ``duplicate``。

设计依据可复现、可解释：每次选择都返回 :class:`SelectionEvidence`，
列出候选数、因晚入库被剔除的记录、因生效于未来被剔除的数量与最终裁决理由。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from .times import to_iso

# ---------------------------------------------------------------------------
# 原因码
# ---------------------------------------------------------------------------
REASON_SELECTED = "SELECTED"
REASON_MISSING_KEY = "MISSING_KEY"              # 该实体从未有过此特征
REASON_ALL_FUTURE = "ALL_FUTURE"                # 有记录但生效时间都晚于事件
REASON_ALL_LATE = "ALL_LATE"                    # 生效时间满足，但全部晚入库
REASON_DUPLICATE = "DUPLICATE"                  # 键完全相同的重复记录


@dataclass(frozen=True)
class FeatureRecord:
    """一条不可变特征记录。

    Attributes:
        entity_id: 实体标识，如客户/账户 ID。
        feature_name: 特征名。
        value: 特征值（浮点）。
        effective_time: 业务生效时间，整数毫秒 epoch。
        ingest_time: 入库可见时间，整数毫秒 epoch。
        version: 显式版本号，同生效时间的最终裁决依据之一。
        record_id: 可选记录 ID，便于在依据中追踪。
    """

    entity_id: str
    feature_name: str
    value: float
    effective_time: int
    ingest_time: int
    version: int = 1
    record_id: Optional[str] = None


@dataclass(frozen=True)
class Event:
    entity_id: str
    event_time: int
    label: Optional[float] = None


@dataclass(frozen=True)
class ExcludedRecord:
    """被剔除候选的简要信息（用于解释"为什么没选它"）。"""

    record_id: Optional[str]
    version: int
    effective_time: int
    ingest_time: int
    value: float
    cause: str  # "LATE_INGEST" | "FUTURE_EFFECTIVE" | "TIE_LOST" | "DUPLICATE"


@dataclass(frozen=True)
class SelectionEvidence:
    """一次特征选择的完整依据。"""

    entity_id: str
    feature_name: str
    event_time: int
    reason: str
    selected_record_id: Optional[str] = None
    selected_version: Optional[int] = None
    selected_value: Optional[float] = None
    selected_effective_time: Optional[int] = None
    selected_ingest_time: Optional[int] = None
    candidates_total: int = 0          # 该 (实体, 特征) 的全部记录数
    candidates_asof: int = 0           # effective_time <= event_time 的数量
    excluded: tuple[ExcludedRecord, ...] = field(default_factory=tuple)
    tie_resolved: bool = False

    def explain(self) -> str:
        """生成人类可读的单行裁决说明。"""
        base = (
            f"[entity={self.entity_id} feature={self.feature_name}] "
            f"event_time={to_iso(self.event_time)}: {self.reason}"
        )
        if self.reason in (REASON_SELECTED, REASON_DUPLICATE):
            base += (
                f" -> record={self.selected_record_id} v{self.selected_version} "
                f"value={self.selected_value} "
                f"(effective={to_iso(self.selected_effective_time)}, "
                f"ingest={to_iso(self.selected_ingest_time)})"
            )
        late = [e for e in self.excluded if e.cause == "LATE_INGEST"]
        if late:
            base += (
                f" | {len(late)} 条事后修订(ingest>event_time)被剔除，"
                f"防止泄漏: "
                + ", ".join(f"{r.record_id or 'v'+str(r.version)}@ingest={to_iso(r.ingest_time)}"
                            for r in late)
            )
        if self.tie_resolved:
            lost = [e for e in self.excluded if e.cause == "TIE_LOST"]
            base += (
                " | 同生效时间多版本，按 入库最晚>版本号最高 裁决，落选: "
                + ", ".join(f"{r.record_id or 'v'+str(r.version)}" for r in lost)
            )
        return base


@dataclass(frozen=True)
class JoinConfig:
    """连接配置。

    Attributes:
        use_event_as_of: 为 True 时以事件时刻作为可见性截止（防泄漏，默认）；
            为 False 时退化为只按 ``effective_time`` 的朴素 as-of join，
            会泄漏事后修订 —— 仅用于对照实验。
        features: 只连接指定特征；None 表示连接特征库中的全部特征。
    """

    use_event_as_of: bool = True
    features: Optional[tuple[str, ...]] = None


class FeatureStore:
    """不可变特征库；按 (实体, 特征) 分组并预排序，查询走 NumPy 向量化选择。"""

    def __init__(self, records: list[FeatureRecord]) -> None:
        self._records: tuple[FeatureRecord, ...] = tuple(records)
        self._all_features: tuple[str, ...] = tuple(
            dict.fromkeys(r.feature_name for r in self._records)
        )
        self._index: dict[tuple[str, str], dict[str, np.ndarray]] = {}
        self._build_index()

    @property
    def all_features(self) -> tuple[str, ...]:
        return self._all_features

    @property
    def records(self) -> tuple[FeatureRecord, ...]:
        return self._records

    def _build_index(self) -> None:
        groups: dict[tuple[str, str], list[FeatureRecord]] = {}
        for rec in self._records:
            groups.setdefault((rec.entity_id, rec.feature_name), []).append(rec)

        for key, rows in groups.items():
            # 排序键：生效时间 -> 入库时间 -> 版本号（全部升序）
            order = sorted(
                range(len(rows)),
                key=lambda i: (rows[i].effective_time, rows[i].ingest_time, rows[i].version),
            )
            sorted_rows = [rows[i] for i in order]
            self._index[key] = {
                "effective": np.asarray([r.effective_time for r in sorted_rows], dtype=np.int64),
                "ingest": np.asarray([r.ingest_time for r in sorted_rows], dtype=np.int64),
                "version": np.asarray([r.version for r in sorted_rows], dtype=np.int64),
                "value": np.asarray([r.value for r in sorted_rows], dtype=np.float64),
                "rows": np.asarray(sorted_rows, dtype=object),
            }

    # ------------------------------------------------------------------
    def select(
        self, entity_id: str, feature_name: str, event_time: int, use_event_as_of: bool
    ) -> SelectionEvidence:
        """对单个 (实体, 特征, 事件时刻) 执行选择并返回依据。"""
        group = self._index.get((entity_id, feature_name))
        if group is None:
            return SelectionEvidence(
                entity_id=entity_id,
                feature_name=feature_name,
                event_time=event_time,
                reason=REASON_MISSING_KEY,
            )

        effective: np.ndarray = group["effective"]
        ingest: np.ndarray = group["ingest"]
        excluded: list[ExcludedRecord] = []

        # 第一步：as-of 生效时间，searchsorted 得到前缀 [0, cut)
        cut = int(np.searchsorted(effective, event_time, side="right"))
        future_rows = group["rows"][cut:]
        for rec in future_rows:
            excluded.append(_excluded(rec, "FUTURE_EFFECTIVE"))

        if cut == 0:
            return SelectionEvidence(
                entity_id=entity_id,
                feature_name=feature_name,
                event_time=event_time,
                reason=REASON_ALL_FUTURE,
                candidates_total=int(effective.size),
                candidates_asof=0,
                excluded=tuple(excluded),
            )

        # 第二步：入库可见性（防事后修订泄漏的关键）
        prefix_ingest = ingest[:cut]
        if use_event_as_of:
            visible = prefix_ingest <= event_time
        else:  # 对照模式：忽略入库时间
            visible = np.ones(cut, dtype=bool)

        for local_idx in np.nonzero(~visible)[0]:
            excluded.append(_excluded(group["rows"][int(local_idx)], "LATE_INGEST"))

        if not bool(visible.any()):
            return SelectionEvidence(
                entity_id=entity_id,
                feature_name=feature_name,
                event_time=event_time,
                reason=REASON_ALL_LATE,
                candidates_total=int(effective.size),
                candidates_asof=cut,
                excluded=tuple(excluded),
            )

        # 第三步：可见候选中按 (effective, ingest, version) 取最大
        version = group["version"]
        vis_idx = np.nonzero(visible)[0]
        best_eff = effective[vis_idx].max()
        tie_eff = vis_idx[effective[vis_idx] == best_eff]
        best_ing = ingest[tie_eff].max()
        tie_ing = tie_eff[ingest[tie_eff] == best_ing]
        best_ver = version[tie_ing].max()
        winners = tie_ing[version[tie_ing] == best_ver]
        chosen = int(winners[0])
        tie_resolved = tie_eff.size > 1

        # 落选但曾进入最终/中途裁决的候选（解释同时间多版本）
        winner_key = (
            int(effective[chosen]),
            int(ingest[chosen]),
            int(version[chosen]),
        )
        for local_idx in tie_eff:
            li = int(local_idx)
            if li == chosen:
                continue
            rec = group["rows"][li]
            key = (rec.effective_time, rec.ingest_time, rec.version)
            if key == winner_key:
                excluded.append(_excluded(rec, "DUPLICATE"))
            else:
                excluded.append(_excluded(rec, "TIE_LOST"))

        rec: FeatureRecord = group["rows"][chosen]
        return SelectionEvidence(
            entity_id=entity_id,
            feature_name=feature_name,
            event_time=event_time,
            reason=REASON_DUPLICATE if winners.size > 1 else REASON_SELECTED,
            selected_record_id=rec.record_id,
            selected_version=rec.version,
            selected_value=rec.value,
            selected_effective_time=rec.effective_time,
            selected_ingest_time=rec.ingest_time,
            candidates_total=int(effective.size),
            candidates_asof=cut,
            excluded=tuple(excluded),
            tie_resolved=tie_resolved,
        )


def _excluded(rec: FeatureRecord, cause: str) -> ExcludedRecord:
    return ExcludedRecord(
        record_id=rec.record_id,
        version=rec.version,
        effective_time=rec.effective_time,
        ingest_time=rec.ingest_time,
        value=rec.value,
        cause=cause,
    )


# ---------------------------------------------------------------------------
# 批量连接
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class JoinedFrame:
    """连接结果。

    - ``entities`` / ``event_times`` / ``labels`` 为长度 N 的数组；
    - ``values`` 为 {特征名: float64 数组}，缺失/不可见填 NaN；
    - ``evidence`` 为长度 N×F 的选择依据，按 **特征在外、事件在内**
      排列（``evidence[f * N + i]``），每个特征对应连续的 N 条。
    """

    entities: tuple[str, ...]
    event_times: np.ndarray
    labels: np.ndarray
    feature_names: tuple[str, ...]
    values: dict[str, np.ndarray]
    evidence: tuple[SelectionEvidence, ...]

    def design_matrix(self) -> np.ndarray:
        """导出 (N, F) 设计矩阵，供简单模型使用。"""
        return np.column_stack([self.values[f] for f in self.feature_names])

    def summary(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for ev in self.evidence:
            counts[ev.reason] = counts.get(ev.reason, 0) + 1
        return counts


def pit_join(
    events: list[Event] | tuple[Event, ...],
    store: FeatureStore,
    config: JoinConfig | None = None,
) -> JoinedFrame:
    """批量执行时间点连接。"""
    config = config or JoinConfig()
    features = config.features or store.all_features

    entities = tuple(e.entity_id for e in events)
    event_times = np.asarray([e.event_time for e in events], dtype=np.int64)
    labels = np.asarray(
        [np.nan if e.label is None else e.label for e in events], dtype=np.float64
    )

    values: dict[str, np.ndarray] = {}
    evidence: list[SelectionEvidence] = []
    for fname in features:
        col = np.full(len(events), np.nan, dtype=np.float64)
        for i, event in enumerate(events):
            ev = store.select(
                event.entity_id, fname, event.event_time, config.use_event_as_of
            )
            evidence.append(ev)
            if ev.selected_value is not None:
                col[i] = ev.selected_value
        values[fname] = col

    return JoinedFrame(
        entities=entities,
        event_times=event_times,
        labels=labels,
        feature_names=tuple(features),
        values=values,
        evidence=tuple(evidence),
    )
