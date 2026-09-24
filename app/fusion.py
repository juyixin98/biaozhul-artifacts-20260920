"""异步测量融合引擎。

关键语义（不能按到达顺序直接融合）：
1. 所有测量按 (time, seq, sensor_rank, id) 确定全序。
2. 新测量到达时，不就地融合，而是找到其前一个检查点，恢复状态后，
   将该时刻之后的所有测量从检查点开始 *重放*（checkpoint replay）。
3. 迟到窗口 LATE_HORIZON_S（默认 2s）：早于 latest_time - horizon 的测量拒绝。
4. 每次处理后保存状态检查点；缓存随窗口滚动裁剪（始终保留一个可回放锚点）。
5. 创新 NIS 超门限拒绝离群测量，保留完整证据（innovation/S/K/nis）。
"""
from __future__ import annotations

import time as wall_time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from . import ekf
from .config import settings

_SENSOR_RANK = {"odometry": 0, "gnss": 1}
_KEY_TOL = 1e-9


@dataclass(frozen=True)
class MeasKey:
    t: float
    seq: int
    rank: int
    mid: str

    def __lt__(self, other: "MeasKey") -> bool:
        return (self.t, self.seq, self.rank, self.mid) < (
            other.t,
            other.seq,
            other.rank,
            other.mid,
        )

    def __le__(self, other: "MeasKey") -> bool:
        return self < other or self == other


@dataclass
class StoredMeasurement:
    key: MeasKey
    mtype: str
    z: np.ndarray
    R: np.ndarray
    gate_nis: float

    def to_trace_brief(self) -> dict[str, Any]:
        return {
            "id": self.key.mid,
            "type": self.mtype,
            "time": self.key.t,
            "seq": self.key.seq,
        }


@dataclass
class _Checkpoint:
    x: np.ndarray
    P: np.ndarray


def validate_covariance(
    R: np.ndarray,
    sym_tol: float = settings.COV_SYM_TOL,
    psd_tol: float = settings.COV_PSD_TOL,
) -> tuple[bool, dict[str, Any]]:
    """测量协方差必须：有限、2x2、对称、半正定。"""
    detail: dict[str, Any] = {}
    if R.shape != (ekf.MEAS_DIM, ekf.MEAS_DIM):
        detail["problem"] = "shape"
        detail["shape"] = list(R.shape)
        return False, detail
    if not np.all(np.isfinite(R)):
        detail["problem"] = "non_finite"
        return False, detail
    if not ekf.is_symmetric(R, sym_tol):
        detail["problem"] = "asymmetric"
        detail["max_asymmetry"] = float(np.max(np.abs(R - R.T)))
        return False, detail
    ok, min_eig = ekf.is_psd(R, psd_tol)
    detail["min_eigenvalue"] = min_eig
    if not ok:
        detail["problem"] = "not_psd"
        return False, detail
    return True, detail


class FusionEngine:
    def __init__(
        self,
        *,
        horizon_s: float | None = None,
        q: float | None = None,
        gate_nis: float | None = None,
        max_rejections: int | None = None,
        init_pos_var: float | None = None,
        init_vel_var: float | None = None,
    ) -> None:
        self.horizon_s = (
            horizon_s if horizon_s is not None else settings.LATE_HORIZON_S
        )
        self.q = q if q is not None else settings.PROCESS_NOISE_Q
        self.gate_nis = gate_nis if gate_nis is not None else settings.GATE_NIS
        self.max_rejections = (
            max_rejections if max_rejections is not None else settings.MAX_REJECTIONS
        )
        self.init_pos_var = (
            init_pos_var if init_pos_var is not None else settings.INIT_POS_VAR
        )
        self.init_vel_var = (
            init_vel_var if init_vel_var is not None else settings.INIT_VEL_VAR
        )

        self.x: np.ndarray | None = None
        self.P: np.ndarray | None = None
        self.latest_time: float | None = None

        self._buffer: list[StoredMeasurement] = []
        self._ids: set[str] = set()
        self._checkpoints: dict[MeasKey, _Checkpoint] = {}
        self._trace: dict[str, dict[str, Any]] = {}
        self._rejections: deque[dict[str, Any]] = deque(maxlen=self.max_rejections)

    # ------------------------------------------------------------------ utils
    def reset(self) -> None:
        self.__init__(
            horizon_s=self.horizon_s,
            q=self.q,
            gate_nis=self.gate_nis,
            max_rejections=self.max_rejections,
            init_pos_var=self.init_pos_var,
            init_vel_var=self.init_vel_var,
        )

    @staticmethod
    def _key(mtype: str, t: float, seq: int, mid: str) -> MeasKey:
        return MeasKey(float(t), int(seq), _SENSOR_RANK[mtype], mid)

    def _record_rejection(
        self,
        reason: str,
        detail: dict[str, Any],
        *,
        mid: str | None = None,
        mtype: str | None = None,
        t: float | None = None,
    ) -> dict[str, Any]:
        entry = {
            "id": mid,
            "time": t,
            "type": mtype,
            "reason": reason,
            "detail": detail,
            "wall_time": wall_time.time(),
        }
        self._rejections.appendleft(entry)
        return entry

    def _state_payload(self) -> dict[str, Any] | None:
        if self.x is None or self.P is None:
            return None
        return {
            "time": self.latest_time,
            "x": [float(v) for v in self.x],
            "P": [[float(v) for v in row] for row in self.P],
            "position": [float(self.x[0]), float(self.x[1])],
            "velocity": [float(self.x[2]), float(self.x[3])],
        }

    # ------------------------------------------------------------------ ingest
    def ingest(
        self,
        *,
        mid: str,
        mtype: str,
        t: float,
        measurement: list[float] | np.ndarray,
        R: list[list[float]] | np.ndarray,
        seq: int = 0,
        gate_nis: float | None = None,
    ) -> dict[str, Any]:
        z = np.asarray(measurement, dtype=np.float64).reshape(ekf.MEAS_DIM)
        Rm = np.asarray(R, dtype=np.float64).reshape(ekf.MEAS_DIM, ekf.MEAS_DIM)
        key = self._key(mtype, t, seq, mid)
        common = {
            "id": mid,
            "type": mtype,
            "time": t,
            "seq": seq,
            "latest_time": self.latest_time,
            "horizon_s": self.horizon_s,
        }

        # 1) 测量协方差合法性（不合法直接拒绝并留证据，绝不“修复后使用”）
        ok, cov_detail = validate_covariance(Rm)
        if not ok:
            evidence = self._record_rejection(
                "BAD_COVARIANCE", cov_detail, mid=mid, mtype=mtype, t=t
            )
            return {
                **common,
                "status": "rejected",
                "accepted": False,
                "reason": "BAD_COVARIANCE",
                "evidence": evidence,
                "state": self._state_payload(),
                "replayed": 0,
            }

        # 2) 去重（幂等）
        if mid in self._ids:
            return {
                **common,
                "status": "duplicate",
                "accepted": bool(self._trace[mid]["accepted"]),
                "reason": "DUPLICATE",
                "step": self._trace[mid],
                "state": self._state_payload(),
                "replayed": 0,
            }

        # 3) 迟到窗口：超过 2s（含容差）直接拒绝，不进入缓存
        if (
            self.x is not None
            and self.latest_time is not None
            and self.latest_time - t > self.horizon_s + _KEY_TOL
        ):
            detail = {
                "late_by_s": float(self.latest_time - t),
                "horizon_s": self.horizon_s,
            }
            evidence = self._record_rejection(
                "LATE_OUT_OF_HORIZON", detail, mid=mid, mtype=mtype, t=t
            )
            return {
                **common,
                "status": "rejected",
                "accepted": False,
                "reason": "LATE_OUT_OF_HORIZON",
                "evidence": evidence,
                "state": self._state_payload(),
                "replayed": 0,
            }

        stored = StoredMeasurement(
            key=key,
            mtype=mtype,
            z=z,
            R=Rm,
            gate_nis=float(gate_nis) if gate_nis is not None else self.gate_nis,
        )

        # 4) 尚未初始化：首条测量直接建立状态
        if self.x is None or self.P is None:
            self._buffer.append(stored)
            self._ids.add(mid)
            self._initialize(stored)
            self.latest_time = key.t
            self._prune()
            return {
                **common,
                "status": "initialized",
                "accepted": True,
                "reason": None,
                "step": self._trace[mid],
                "state": self._state_payload(),
                "replayed": 0,
                "latest_time": self.latest_time,
                "buffered": len(self._buffer),
            }

        # 5) 插入缓存（按全序），再从锚点检查点重放
        self._insert_sorted(stored)
        self._ids.add(mid)

        anchor = self._latest_checkpoint_before(key)
        reinitialized = False
        if anchor is None:
            # 乱序发生在初始化之前：当前初始化点比新消息更晚。
            # 数学上正确的做法是以缓存中最早测量重新初始化，再完整重放。
            first = self._buffer[0]
            self.x = None
            self.P = None
            self._checkpoints.clear()
            self._initialize(first)
            anchor = first.key
            reinitialized = True

        cp = self._checkpoints[anchor]
        self.x = cp.x.copy()
        self.P = cp.P.copy()

        tail = [m for m in self._buffer if m.key > anchor]
        # 若新消息本身成为锚点（最早测量），其后所有消息都是重放
        replayed = len(tail) if anchor == key else max(0, len(tail) - 1)
        target_trace: dict[str, Any] | None = None
        accepted = False
        reason: str | None = None
        status = "processed"

        prev_time = anchor.t
        for sm in tail:
            # 只为本次新到达的消息追加拒绝证据；重放的历史消息不再重复记录
            trace_entry, was_accepted = self._process_step(
                sm, prev_time, record_evidence=(sm.key == key)
            )
            prev_time = sm.key.t
            if sm.key == key:
                target_trace = trace_entry
                accepted = was_accepted
                if not was_accepted:
                    status, reason = "rejected", "OUTLIER_GATE"
                else:
                    status, reason = "processed", None

        if target_trace is None:
            # 新消息成为新的最早测量：它承担了（重新）初始化
            target_trace = self._trace[mid]
            accepted = True
            status = "initialized" if not reinitialized else "processed"

        self.latest_time = self._buffer[-1].key.t
        self._prune()

        return {
            **common,
            "status": status,
            "accepted": accepted,
            "reason": reason,
            "step": target_trace,
            "state": self._state_payload(),
            "replayed": replayed,
            "latest_time": self.latest_time,
            "buffered": len(self._buffer),
        }

    # --------------------------------------------------------------- internal
    def _insert_sorted(self, sm: StoredMeasurement) -> None:
        # 正常路径多为追加；保持严格有序
        if not self._buffer or sm.key > self._buffer[-1].key:
            self._buffer.append(sm)
            return
        for i, existing in enumerate(self._buffer):
            if sm.key < existing.key:
                self._buffer.insert(i, sm)
                return
        self._buffer.append(sm)

    def _latest_checkpoint_before(self, key: MeasKey) -> MeasKey | None:
        candidate: MeasKey | None = None
        for ck in self._checkpoints:
            if ck < key and (candidate is None or ck > candidate):
                candidate = ck
        return candidate

    def _initialize(self, sm: StoredMeasurement) -> None:
        x = np.zeros(ekf.STATE_DIM, dtype=np.float64)
        P = np.zeros((ekf.STATE_DIM, ekf.STATE_DIM), dtype=np.float64)
        if sm.mtype == "gnss":
            x[0:2] = sm.z
            P[0:2, 0:2] = sm.R
            P[2, 2] = self.init_vel_var
            P[3, 3] = self.init_vel_var
        else:  # odometry
            x[2:4] = sm.z
            P[0, 0] = self.init_pos_var
            P[1, 1] = self.init_pos_var
            P[2:4, 2:4] = sm.R
        P, min_eig = ekf.enforce_psd(P)
        self.x, self.P = x, P
        self._checkpoints[sm.key] = _Checkpoint(x.copy(), P.copy())
        H = ekf.H_GNSS if sm.mtype == "gnss" else ekf.H_ODO
        innovation = np.zeros(ekf.MEAS_DIM)
        S = sm.R.copy()
        self._trace[sm.key.mid] = {
            **sm.to_trace_brief(),
            "dt": 0.0,
            "predicted": {"time": sm.key.t, "x": [float(v) for v in x]},
            "P_predicted": [[float(v) for v in row] for row in P],
            "innovation": [0.0, 0.0],
            "S": [[float(v) for v in row] for row in S],
            "K": [[0.0] * ekf.STATE_DIM for _ in range(ekf.MEAS_DIM)],
            "nis": 0.0,
            "gate_nis": sm.gate_nis,
            "accepted": True,
            "gated": False,
            "posterior": {"time": sm.key.t, "x": [float(v) for v in x]},
            "P": [[float(v) for v in row] for row in P],
            "post_min_eigenvalue": min_eig,
            "initialized_here": True,
        }

    def _process_step(
        self, sm: StoredMeasurement, prev_time: float, *, record_evidence: bool = True
    ) -> tuple[dict[str, Any], bool]:
        dt = sm.key.t - prev_time
        H = ekf.H_GNSS if sm.mtype == "gnss" else ekf.H_ODO

        x_pred, P_pred = ekf.predict(self.x, self.P, dt, self.q)
        result = ekf.update(x_pred, P_pred, sm.z, sm.R, H)

        nis = result["nis"]
        accepted = nis <= sm.gate_nis
        if accepted:
            self.x, self.P = result["x"], result["P"]
        else:
            # 时间照常前推（预测保留），仅拒绝测量修正
            self.x, self.P = x_pred, P_pred
            if record_evidence:
                self._record_rejection(
                    "OUTLIER_GATE",
                    {
                        "nis": nis,
                        "gate_nis": sm.gate_nis,
                        "innovation": [float(v) for v in result["innovation"]],
                        "S": [[float(v) for v in row] for row in result["S"]],
                    },
                    mid=sm.key.mid,
                    mtype=sm.mtype,
                    t=sm.key.t,
                )

        self._checkpoints[sm.key] = _Checkpoint(self.x.copy(), self.P.copy())
        entry = {
            **sm.to_trace_brief(),
            "dt": float(dt),
            "predicted": {
                "time": prev_time + dt,
                "x": [float(v) for v in x_pred],
            },
            "P_predicted": [[float(v) for v in row] for row in P_pred],
            "innovation": [float(v) for v in result["innovation"]],
            "S": [[float(v) for v in row] for row in result["S"]],
            "K": [[float(v) for v in row] for row in result["K"]],
            "nis": nis,
            "gate_nis": sm.gate_nis,
            "accepted": accepted,
            "gated": not accepted,
            "posterior": {
                "time": sm.key.t,
                "x": [float(v) for v in self.x],
            },
            "P": [[float(v) for v in row] for row in self.P],
            "pre_min_eigenvalue": result["pre_min_eig"],
            "post_min_eigenvalue": result["post_min_eig"],
            "initialized_here": False,
        }
        self._trace[sm.key.mid] = entry
        return entry, accepted

    def _prune(self) -> None:
        """按迟到窗口滚动裁剪，始终保留一个不晚于窗口下沿的锚点检查点。"""
        if not self._checkpoints or self.latest_time is None:
            return
        floor = self.latest_time - self.horizon_s
        keys_sorted = sorted(self._checkpoints.keys())
        base = keys_sorted[0]
        for k in keys_sorted:
            if k.t <= floor + _KEY_TOL:
                base = k
            else:
                break

        for k in keys_sorted:
            if k < base:
                del self._checkpoints[k]

        removed_ids: set[str] = set()
        kept_buffer: list[StoredMeasurement] = []
        for sm in self._buffer:
            if sm.key < base:
                removed_ids.add(sm.key.mid)
            else:
                kept_buffer.append(sm)
        self._buffer = kept_buffer
        # 出窗检查点与出窗测量一一对应（base 锚点本身保留）
        for mid in removed_ids:
            self._trace.pop(mid, None)
            self._ids.discard(mid)

    # ----------------------------------------------------------------- views
    def state_view(self) -> dict[str, Any]:
        return {
            "initialized": self.x is not None,
            "latest_time": self.latest_time,
            "state": self._state_payload(),
            "buffered": len(self._buffer),
            "checkpoints": len(self._checkpoints),
        }

    def trace_view(self, limit: int = 500) -> list[dict[str, Any]]:
        entries = sorted(
            self._trace.values(),
            key=lambda e: (e["time"], e["seq"], _SENSOR_RANK[e["type"]], e["id"]),
        )
        return entries[-limit:]

    def rejections_view(self, limit: int = 100) -> list[dict[str, Any]]:
        return list(self._rejections)[:limit]
