"""多目标跟踪器：匀速 Kalman 预测 + Mahalanobis 门控 + 匈牙利分配。

设计要点
--------
* 每条轨迹（Track）维护独立的 Kalman 状态、连续命中/丢失计数与状态
  （tentative 待确认 / confirmed 已确认 / coasting 丢测滑行 / deleted）。
* 每步：先按真实 dt 对所有存活轨迹做时间更新，再与聚合后的检测构建
  代价矩阵，平方 Mahalanobis 距离超过门限的候选禁止分配，最后由
  ``scipy.optimize.linear_sum_assignment`` 求全局最优一一对应。
* 新轨迹命中 ``confirm_hits`` 帧后确认；连续丢失 ``max_misses + 1``
  帧（即连续 max_misses 帧预测后仍未找回）删除。
* 帧内重复检测（同一目标多个近似框）用并查集聚合为一个量测。
* 乱序帧（frame_id 非严格递增，或 timestamp 非递增）直接抛异常拒绝，
  关联代码路径不接受任何真值 ID。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

import numpy as np
from scipy.optimize import linear_sum_assignment

from .kalman import ConstantVelocity2D

# 自由度 2 的卡方门限（约 99% 与 95% 置信水平）
CHI2_99_2DOF = 9.210
CHI2_95_2DOF = 5.991


class TrackState(str, Enum):
    TENTATIVE = "tentative"
    CONFIRMED = "confirmed"
    COASTING = "coasting"
    DELETED = "deleted"


class StaleFrameError(ValueError):
    """乱序帧：frame_id 或 timestamp 未严格递增。"""


@dataclass
class Detection:
    """聚合后的一个二维点检测。"""

    x: float
    y: float
    member_ids: tuple[str, ...] = ()

    @property
    def z(self) -> np.ndarray:
        return np.array([self.x, self.y], dtype=np.float64)


@dataclass
class AssignmentRecord:
    """单个 检测-轨迹 候选对的门控/选择审计信息。"""

    track_id: int
    detection_index: int
    predicted_xy: tuple[float, float]
    measurement_xy: tuple[float, float]
    euclidean_distance: float
    mahalanobis_sq: float
    gate_threshold: float
    within_gate: bool
    selected: bool

    def to_dict(self) -> dict:
        return {
            "track_id": self.track_id,
            "detection_index": self.detection_index,
            "predicted_xy": list(self.predicted_xy),
            "measurement_xy": list(self.measurement_xy),
            "euclidean_distance": self.euclidean_distance,
            "mahalanobis_sq": self.mahalanobis_sq,
            "gate_threshold": self.gate_threshold,
            "within_gate": self.within_gate,
            "selected": self.selected,
        }


@dataclass
class TrackSnapshot:
    """本步对外输出的轨迹状态。"""

    track_id: int
    state: str
    hits: int
    hit_streak: int
    misses: int
    position: tuple[float, float]
    velocity: tuple[float, float]
    position_variance: tuple[float, float]
    associated_detection_index: int | None
    age_frames: int

    def to_dict(self) -> dict:
        return {
            "track_id": self.track_id,
            "state": self.state,
            "hits": self.hits,
            "hit_streak": self.hit_streak,
            "misses": self.misses,
            "position": list(self.position),
            "velocity": list(self.velocity),
            "position_variance": list(self.position_variance),
            "associated_detection_index": self.associated_detection_index,
            "age_frames": self.age_frames,
        }


@dataclass
class Track:
    track_id: int
    kf: ConstantVelocity2D
    x: np.ndarray
    P: np.ndarray
    hits: int = 1
    hit_streak: int = 1
    misses: int = 0
    age: int = 1
    state: TrackState = TrackState.TENTATIVE
    ever_confirmed: bool = False
    last_detection_index: int | None = None

    @property
    def alive(self) -> bool:
        return self.state is not TrackState.DELETED

    def snapshot(self) -> TrackSnapshot:
        return TrackSnapshot(
            track_id=self.track_id,
            state=self.state.value,
            hits=self.hits,
            hit_streak=self.hit_streak,
            misses=self.misses,
            position=(float(self.x[0]), float(self.x[1])),
            velocity=(float(self.x[2]), float(self.x[3])),
            position_variance=(float(self.P[0, 0]), float(self.P[1, 1])),
            associated_detection_index=self.last_detection_index,
            age_frames=self.age,
        )

@dataclass
class TrackerConfig:
    confirm_hits: int = 3
    max_misses: int = 5
    gate_threshold: float = CHI2_99_2DOF
    merge_radius: float = 0.25
    process_noise: float = 1.0
    measurement_var: float = 1.0
    init_pos_var: float = 10.0
    init_vel_var: float = 100.0

    def __post_init__(self) -> None:
        if self.confirm_hits < 1:
            raise ValueError("confirm_hits 必须 >= 1")
        if self.max_misses < 1:
            raise ValueError("max_misses 必须 >= 1")
        if self.gate_threshold <= 0:
            raise ValueError("gate_threshold 必须为正")
        if self.merge_radius < 0:
            raise ValueError("merge_radius 不能为负")


@dataclass
class FrameResult:
    frame_id: int
    timestamp: float
    dt: float | None
    detections: list[Detection]
    merged_groups: list[list[str]]
    tracks: list[TrackSnapshot]
    assignments: list[AssignmentRecord]
    rejected_by_gate: list[AssignmentRecord]
    births: list[int]
    matched: list[tuple[int, int]]
    coasted: list[int]
    deleted: list[int]
    next_track_id: int
    selection_basis: dict = field(default_factory=dict)


class Tracker:
    """逐帧多目标关联器（非线程安全，外层 Session 加锁）。"""

    def __init__(self, config: TrackerConfig | None = None) -> None:
        self.config = config or TrackerConfig()
        self._kf = ConstantVelocity2D(
            q=self.config.process_noise,
            r=self.config.measurement_var,
            p_pos=self.config.init_pos_var,
            p_vel=self.config.init_vel_var,
        )
        self.tracks: dict[int, Track] = {}
        self._next_id = 1
        self._last_frame_id: int | None = None
        self._last_timestamp: float | None = None

    # ------------------------------------------------------------- utilities
    def _merge_detections(
        self, raw: list[tuple[float, float, str | None]]
    ) -> tuple[list[Detection], list[list[str]]]:
        """帧内重复检测聚合：欧氏距离 <= merge_radius 的点用并查集合并。"""
        n = len(raw)
        parent = list(range(n))

        def find(a: int) -> int:
            while parent[a] != a:
                parent[a] = parent[parent[a]]
                a = parent[a]
            return a

        def union(a: int, b: int) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra

        pts = np.array([[p[0], p[1]] for p in raw], dtype=np.float64)
        r2 = self.config.merge_radius**2
        for i in range(n):
            for j in range(i + 1, n):
                if float(((pts[i] - pts[j]) ** 2).sum()) <= r2:
                    union(i, j)

        groups: dict[int, list[int]] = {}
        for i in range(n):
            groups.setdefault(find(i), []).append(i)

        detections: list[Detection] = []
        merged_groups: list[list[str]] = []
        for members in groups.values():
            members.sort()
            ids = tuple(
                raw[m][2] if raw[m][2] is not None else f"anon#{m}"
                for m in members
            )
            cx = float(pts[members, 0].mean())
            cy = float(pts[members, 1].mean())
            detections.append(Detection(x=cx, y=cy, member_ids=ids))
            merged_groups.append(list(ids))
        return detections, merged_groups

    def _spawn(self, det: Detection) -> Track:
        x, P = self._kf.initialize(det.z)
        track = Track(track_id=self._next_id, kf=self._kf, x=x, P=P)
        self.tracks[self._next_id] = track
        self._next_id += 1
        return track

    # ------------------------------------------------------------------ step
    def step(
        self,
        frame_id: int,
        timestamp: float,
        raw_detections: list[tuple[float, float, str | None]],
    ) -> FrameResult:
        """处理一帧。

        raw_detections: (x, y, detection_id) 三元组列表。
        乱序（frame_id / timestamp 非严格递增）抛 :class:`StaleFrameError`。
        """
        # 1) 时序校验：严格递增，乱序明确拒绝
        if self._last_frame_id is not None:
            if frame_id <= self._last_frame_id:
                raise StaleFrameError(
                    f"frame_id 必须严格递增：已处理 "
                    f"{self._last_frame_id}，收到 {frame_id}"
                )
            if timestamp < self._last_timestamp:
                raise StaleFrameError(
                    f"timestamp 不得回退：上一帧 {self._last_timestamp}，"
                    f"收到 {timestamp}"
                )
        dt: float | None = (
            None
            if self._last_timestamp is None
            else float(timestamp) - float(self._last_timestamp)
        )
        if dt is not None and dt <= 0:
            raise StaleFrameError(
                f"同时间戳的后续帧不允许关联（dt={dt}）"
            )

        # 2) 帧内重复检测聚合
        detections, merged_groups = self._merge_detections(raw_detections)

        # 3) 时间更新（预测）——dt 真实参与状态转移
        predicted: dict[int, tuple[np.ndarray, np.ndarray]] = {}
        if dt is not None:
            for tr in self.tracks.values():
                if not tr.alive:
                    continue
                tr.x, tr.P = self._kf.predict(tr.x, tr.P, dt)
                predicted[tr.track_id] = (tr.x, tr.P)
        else:
            for tr in self.tracks.values():
                predicted[tr.track_id] = (tr.x, tr.P)

        alive = [t for t in self.tracks.values() if t.alive]
        n_tracks, n_det = len(alive), len(detections)

        # 4) 代价矩阵：平方 Mahalanobis 距离；门控外置为禁止
        records: list[AssignmentRecord] = []
        cost = np.full((n_tracks, n_det), np.inf, dtype=np.float64)
        allowed = np.zeros((n_tracks, n_det), dtype=bool)
        gate = float(self.config.gate_threshold)
        for i, tr in enumerate(alive):
            x_pred, P_pred = predicted[tr.track_id]
            for j, det in enumerate(detections):
                innovation = det.z - x_pred[:2]
                S = P_pred[:2, :2] + self._kf.R
                d2 = ConstantVelocity2D.mahalanobis_sq(innovation, S)
                eu = ConstantVelocity2D.euclidean(innovation)
                ok = bool(d2 <= gate)
                rec = AssignmentRecord(
                    track_id=tr.track_id,
                    detection_index=j,
                    predicted_xy=(float(x_pred[0]), float(x_pred[1])),
                    measurement_xy=(det.x, det.y),
                    euclidean_distance=eu,
                    mahalanobis_sq=d2,
                    gate_threshold=gate,
                    within_gate=ok,
                    selected=False,
                )
                records.append(rec)
                if ok:
                    cost[i, j] = d2
                    allowed[i, j] = True

        # 5) 匈牙利全局最优分配（仅在门控允许的候选上）
        matched_pairs: list[tuple[int, int]] = []
        if n_tracks > 0 and n_det > 0 and allowed.any():
            big = cost[np.isfinite(cost)].max() + 1e6 if np.isfinite(cost).any() else 1e6
            work = np.where(np.isfinite(cost), cost, big)
            row_ind, col_ind = linear_sum_assignment(work)
            for r, c in zip(row_ind, col_ind):
                if allowed[r, c]:
                    matched_pairs.append((int(alive[r].track_id), int(c)))
                    records[r * n_det + c].selected = True

        matched_track_ids = {tid for tid, _ in matched_pairs}
        matched_det_idx = {j for _, j in matched_pairs}

        # 6) 量测更新 / 丢测滑行 / 新生
        births: list[int] = []
        coasted: list[int] = []
        deleted: list[int] = []

        for tid, j in matched_pairs:
            tr = self.tracks[tid]
            x_pred, P_pred = predicted[tid]
            tr.x, tr.P, _, _ = self._kf.update(x_pred, P_pred, detections[j].z)
            tr.hits += 1
            tr.hit_streak += 1
            tr.misses = 0
            tr.last_detection_index = j
            if tr.state == TrackState.TENTATIVE and tr.hit_streak >= self.config.confirm_hits:
                tr.state = TrackState.CONFIRMED
                tr.ever_confirmed = True
            elif tr.state == TrackState.COASTING and tr.ever_confirmed:
                # 已确认轨迹滑行后被找回，恢复 confirmed（身份保持）
                tr.state = TrackState.CONFIRMED

        for tr in alive:
            if tr.track_id in matched_track_ids:
                continue
            # 无检测关联：丢测计数 +1，连续命中中断
            tr.misses += 1
            tr.hit_streak = 0
            tr.last_detection_index = None
            if tr.misses > self.config.max_misses:
                tr.state = TrackState.DELETED
                deleted.append(tr.track_id)
            elif tr.state == TrackState.TENTATIVE:
                # 待确认轨迹丢测：保持 tentative（尚未连续命中）
                coasted.append(tr.track_id)
            else:
                tr.state = TrackState.COASTING
                coasted.append(tr.track_id)

        for j, det in enumerate(detections):
            if j in matched_det_idx:
                continue
            newborn = self._spawn(det)
            births.append(newborn.track_id)

        # 仅对本步之前就存在的轨迹累计年龄（新生轨迹初始化时 age=1）
        for tr in alive:
            tr.age += 1

        # 7) 输出
        snapshots = [t.snapshot() for t in self.tracks.values() if t.alive]
        rejected = [r for r in records if not r.within_gate]
        result = FrameResult(
            frame_id=frame_id,
            timestamp=float(timestamp),
            dt=dt,
            detections=detections,
            merged_groups=merged_groups,
            tracks=snapshots,
            assignments=sorted(records, key=lambda r: (r.track_id, r.detection_index)),
            rejected_by_gate=sorted(
                rejected, key=lambda r: (r.track_id, r.detection_index)
            ),
            births=births,
            matched=matched_pairs,
            coasted=coasted,
            deleted=deleted,
            next_track_id=self._next_id,
            selection_basis={
                "method": "global_optimal_hungarian",
                "cost": "mahalanobis_sq",
                "gate": "chi_squared_2dof",
                "gate_threshold": gate,
                "assignment_rule": "scipy.optimize.linear_sum_assignment on "
                "gate-allowed costs; one-to-one; unmatched detections spawn "
                "tentative tracks; unmatched tracks coast",
                "state_transition": f"constant_velocity, dt={dt}",
            },
        )

        self._last_frame_id = frame_id
        self._last_timestamp = float(timestamp)
        return result
