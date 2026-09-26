"""端到端编排：合成数据 → 时钟校正 → 在线/离线配对 → 对照。"""

from dataclasses import dataclass

from .clock import estimate_offset
from .matcher import MessageMatcher
from .models import Match, Reject
from .offline import offline_pair
from .synthetic import (
    SensorEvent,
    arrival_order,
    calibration_pairs,
    events_to_messages,
    generate_trajectory,
    make_sensor_events,
    required_out_of_orderness,
)


@dataclass(frozen=True)
class PairingOutput:
    online_matches: list[Match]
    online_rejects: list[Reject]
    offline_matches: list
    offline_unmatched_a: tuple[str, ...]
    offline_unmatched_b: tuple[str, ...]
    estimated_offset: float
    consistent: bool

    def as_dict(self) -> dict:
        return {
            "online": {
                "matches": [m.as_dict() for m in self.online_matches],
                "rejects": [r.as_dict() for r in self.online_rejects],
            },
            "offline": {
                "matches": [m.as_dict() for m in self.offline_matches],
                "unmatched": {
                    "a": list(self.offline_unmatched_a),
                    "b": list(self.offline_unmatched_b),
                },
            },
            "clock": {"estimated_offset": self.estimated_offset},
            "consistent": self.consistent,
        }


def _edge_set(matches) -> set[tuple[str, str]]:
    return {(m.id_a, m.id_b) for m in matches}


def run_synthetic_pairing(cfg: dict) -> PairingOutput:
    """按配置字典运行一次完整的合成配对实验。

    配置键见 README 的请求样例；常用键：

    * ``tolerance``: 时间容差（秒），默认 0.05。
    * ``max_buffer_size``: 每流缓存容量，默认 10000（足够大，不溢出）。
    * ``buffer_*``: 各传感器的 rate_hz / clock_offset / jitter /
      delay_mean / delay_jitter / drop_prob / gap_intervals /
      duplicate_spec / seed。
    * ``duration`` / ``traj_dt``: 轨迹时长与离散步长。
    * ``auto_out_of_orderness``: True（默认）时按实际到达顺序自动确定 δ。
    """
    tolerance = float(cfg.get("tolerance", 0.05))
    max_buffer_size = int(cfg.get("max_buffer_size", 10_000))
    auto_ooo = bool(cfg.get("auto_out_of_orderness", True))
    manual_ooo = float(cfg.get("max_out_of_orderness", 0.0))

    traj = generate_trajectory(
        duration=float(cfg.get("duration", 20.0)),
        dt=float(cfg.get("traj_dt", 0.005)),
    )

    def sensor_cfg(key: str) -> dict:
        return dict(cfg.get(key, {}))

    ca = sensor_cfg("sensor_a")
    cb = sensor_cfg("sensor_b")
    ca.setdefault("seed", 11)
    cb.setdefault("seed", 22)

    events_a: list[SensorEvent] = make_sensor_events("a", traj, **ca)
    events_b: list[SensorEvent] = make_sensor_events("b", traj, **cb)

    # 1) 时钟偏移校正：用同时刻校准点对估计 B 钟偏移并施加到 B 流
    cal = cfg.get("calibration", {})
    cal_a, cal_b = calibration_pairs(
        traj,
        n_points=int(cal.get("n_points", 21)),
        clock_offset_b=float(cb.get("clock_offset", 0.0)),
        timestamp_jitter=float(cal.get("jitter", 0.001)),
        seed=int(cal.get("seed", 99)),
    )
    correction = estimate_offset(cal_a, cal_b, trim_ratio=float(cal.get("trim_ratio", 0.1)))
    offset_b = correction.offset  # 校正 B：t_b_corrected = raw_b + offset

    msgs_a = events_to_messages(events_a, 0.0)
    msgs_b = events_to_messages(events_b, offset_b)
    by_id_a = {m.id: m for m in msgs_a}
    by_id_b = {m.id: m for m in msgs_b}

    # 2) 在线：按合成的到达顺序乱序推送
    delta = (
        required_out_of_orderness(events_a, events_b, offset_b=offset_b)
        if auto_ooo
        else manual_ooo
    )
    matcher = MessageMatcher(
        tolerance=tolerance,
        max_buffer_size=max_buffer_size,
        max_out_of_orderness=delta,
    )
    for stream, ev in arrival_order(events_a, events_b):
        msg = by_id_a[ev.message.id] if stream == "a" else by_id_b[ev.message.id]
        matcher.push(stream, msg)
    matcher.flush()

    # 3) 离线权威定义（同一批校正后消息，全知视角）
    offline = offline_pair(
        msgs_a,
        msgs_b,
        tolerance,
        raw_a={},
        raw_b={},
    )

    # 4) 对照：未发生缓存溢出时在线边集必须与离线边集一致
    overflowed = any(r.reason == "buffer_overflow" for r in matcher.rejects)
    consistent = (not overflowed) and _edge_set(matcher.matches) == _edge_set(
        offline.matches
    )

    # 在线结果的 raw 时间戳即校正前读数：用事件原始时间戳回填以便审计
    raw_ts_a = {ev.message.id: ev.message.timestamp for ev in events_a}
    raw_ts_b = {ev.message.id: ev.message.timestamp for ev in events_b}
    online_matches = [
        Match(
            m.id_a,
            m.id_b,
            m.t_a,
            m.t_b,
            raw_ts_a.get(m.id_a, m.raw_a),
            raw_ts_b.get(m.id_b, m.raw_b),
        )
        for m in matcher.matches
    ]

    return PairingOutput(
        online_matches=online_matches,
        online_rejects=list(matcher.rejects),
        offline_matches=[
            Match(
                m.id_a,
                m.id_b,
                m.t_a,
                m.t_b,
                raw_ts_a.get(m.id_a, m.raw_a),
                raw_ts_b.get(m.id_b, m.raw_b),
            )
            for m in offline.matches
        ],
        offline_unmatched_a=offline.unmatched_a,
        offline_unmatched_b=offline.unmatched_b,
        estimated_offset=offset_b,
        consistent=consistent,
    )
