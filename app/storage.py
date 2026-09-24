"""内存会话存储：跟踪器实例 + 帧幂等缓存。

重复投递语义：
* frame_id 已处理且请求体 SHA-256 相同 -> 原样返回缓存响应（replay=True），
  跟踪器状态不前进，轨迹 ID 不增加。
* frame_id 已处理但请求体不同 -> HTTP 409 冲突。
* frame_id 回退或时间戳回退 -> HTTP 422（乱序拒绝）。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from .crypto import new_session_key, sha256_hex, sign_response
from .tracker import Tracker, TrackerConfig


@dataclass
class Session:
    session_id: str
    config: TrackerConfig | None = None
    signing_key_hex: str = field(default_factory=new_session_key)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    # frame_id -> (payload 指纹, 无签名字响应体)
    _frame_cache: dict[int, tuple[str, dict]] = field(default_factory=dict)
    tracker: Tracker = field(init=False)

    def __post_init__(self) -> None:
        self.tracker = Tracker(self.config)


class SessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._global_lock = threading.Lock()

    def create(
        self,
        session_id: str,
        config: TrackerConfig | None = None,
        signing_key_hex: str | None = None,
    ) -> Session:
        with self._global_lock:
            if session_id in self._sessions:
                raise KeyError(f"会话已存在: {session_id}")
            session = Session(
                session_id=session_id,
                config=config,
                signing_key_hex=signing_key_hex or new_session_key(),
            )
            self._sessions[session_id] = session
            return session

    def get(self, session_id: str) -> Session:
        try:
            return self._sessions[session_id]
        except KeyError:
            raise KeyError(f"会话不存在: {session_id}") from None

    def all(self) -> list[Session]:
        return list(self._sessions.values())

    def delete(self, session_id: str) -> None:
        with self._global_lock:
            self._sessions.pop(session_id, None)

    def __len__(self) -> int:
        return len(self._sessions)


def process_frame(
    session: Session,
    payload: dict,
    frame_id: int,
    timestamp: float,
    raw_detections: list[tuple[float, float, str | None]],
) -> tuple[dict, bool]:
    """在会话锁内处理一帧。

    返回 ``(响应体（含签名字段）, replay)``。
    重复 frame_id 但负载不同时抛 :class:`ValueError`（调用方映射为 409）。
    """
    fingerprint = sha256_hex(payload)
    with session._lock:
        cached = session._frame_cache.get(frame_id)
        if cached is not None:
            cached_fp, cached_body = cached
            if cached_fp != fingerprint:
                raise ValueError(
                    f"frame_id={frame_id} 已被不同的请求体处理，拒绝覆盖"
                )
            # 真实验证缓存响应的 HMAC 仍然有效后再重放
            body = dict(cached_body)
            body["replay"] = True
            body["signature"] = sign_response(body, session.signing_key_hex)
            return body, True

        result = session.tracker.step(frame_id, timestamp, raw_detections)
        body = _result_to_body(session.session_id, result)
        body["replay"] = False
        body["signature"] = sign_response(body, session.signing_key_hex)
        body["signature_algorithm"] = (
            "HMAC-SHA256(canonical_json(body_without_signature))"
        )
        # 缓存不含签名的响应体（签名每次现算，密钥不变则结果一致）
        cache_body = {k: v for k, v in body.items() if k != "signature"}
        session._frame_cache[frame_id] = (fingerprint, cache_body)
        return body, False


def _result_to_body(session_id: str, result) -> dict:
    return {
        "session_id": session_id,
        "frame_id": result.frame_id,
        "timestamp": result.timestamp,
        "dt": result.dt,
        "detections": [
            {
                "index": i,
                "x": d.x,
                "y": d.y,
                "member_ids": list(d.member_ids),
            }
            for i, d in enumerate(result.detections)
        ],
        "merged_groups": result.merged_groups,
        "tracks": [t.to_dict() for t in result.tracks],
        "assignments": [a.to_dict() for a in result.assignments],
        "rejected_by_gate": [a.to_dict() for a in result.rejected_by_gate],
        "births": result.births,
        "matched": [list(p) for p in result.matched],
        "coasted": result.coasted,
        "deleted": result.deleted,
        "next_track_id": result.next_track_id,
        "selection_basis": result.selection_basis,
        "replay": False,
        "signature_algorithm": (
            "HMAC-SHA256(canonical_json(body_without_signature))"
        ),
    }
