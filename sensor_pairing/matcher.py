"""核心时间配对器.

离线配对定义(确定性, 可对照手工演算):

1. 消息按**到达顺序**逐条处理(允许时间戳乱序).
2. 流 B 的时间戳先做时钟偏移校正: t_b' = t_b - clock_offset_b,
   其中 clock_offset_b 表示 B 时钟相对 A 时钟的领先量(秒).
3. 消息 m 到达时, 在对方流的缓存中寻找候选:
   - 候选须满足 |t_m - t_c| <= tolerance(最近匹配窗口);
   - 若有多个候选, 选择 |dt| 最小者(最近匹配);
   - 平局规则: 先比较候选校正后时间戳(较早者优先),
     再比较到达序号(先到达者优先);
   - 单次消费: 每条消息最多参与一对, 配对后立即从缓存移除.
4. 若无可配对候选, m 进入本流缓存. 缓存容量为 cache_size,
   超出时挤出**最早到达**的缓存消息, 记为 EVICTED_CACHE_FULL(过期).
5. finish() 时仍滞留缓存的消息记为 NO_MATCH_WITHIN_TOLERANCE.
"""

from __future__ import annotations

from dataclasses import dataclass

from sensor_pairing.models import (
    MatchResult,
    Pair,
    SensorMessage,
    StreamId,
    UnmatchedMessage,
    UnmatchedReason,
)


@dataclass
class _Buffered:
    """缓存中的消息及其配对键."""

    message: SensorMessage
    corrected_t: float
    seq: int  # 全局到达序号, 用于平局裁决与先进先出挤出


class TimePairingMatcher:
    """按时间容差配对两路传感器消息的离线匹配器."""

    def __init__(
        self,
        tolerance: float,
        cache_size: int,
        clock_offset_b: float = 0.0,
    ) -> None:
        if tolerance < 0:
            raise ValueError("tolerance 必须非负")
        if cache_size < 1:
            raise ValueError("cache_size 必须 >= 1")
        self._tolerance = float(tolerance)
        self._cache_size = int(cache_size)
        self._clock_offset_b = float(clock_offset_b)
        self._buffers: dict[StreamId, list[_Buffered]] = {
            StreamId.A: [],
            StreamId.B: [],
        }
        self._result = MatchResult()
        self._seq = 0
        self._finished = False

    def _correct(self, stream: StreamId, timestamp: float) -> float:
        """应用时钟偏移校正, 得到统一时基下的时间戳."""
        if stream is StreamId.B:
            return timestamp - self._clock_offset_b
        return timestamp

    def add(self, stream: StreamId, message: SensorMessage) -> None:
        """按到达顺序喂入一条消息."""
        if self._finished:
            raise RuntimeError("finish() 之后不能再添加消息")
        entry = _Buffered(
            message=message,
            corrected_t=self._correct(stream, message.timestamp),
            seq=self._seq,
        )
        self._seq += 1

        best = self._find_nearest(stream.opposite, entry.corrected_t)
        if best is not None:
            self._buffers[stream.opposite].remove(best)
            self._result.pairs.append(self._make_pair(stream, entry, best))
            return

        own = self._buffers[stream]
        own.append(entry)
        if len(own) > self._cache_size:
            evicted = own.pop(0)  # 最早到达者过期
            self._result.unmatched.append(
                UnmatchedMessage(
                    stream=stream,
                    message=evicted.message,
                    reason=UnmatchedReason.EVICTED_CACHE_FULL,
                )
            )

    def _find_nearest(
        self, stream: StreamId, corrected_t: float
    ) -> _Buffered | None:
        """在指定流缓存中找容差内的最近候选(含平局规则)."""
        candidates = [
            e
            for e in self._buffers[stream]
            if abs(e.corrected_t - corrected_t) <= self._tolerance
        ]
        if not candidates:
            return None
        return min(
            candidates,
            key=lambda e: (abs(e.corrected_t - corrected_t), e.corrected_t, e.seq),
        )

    def _make_pair(self, incoming: StreamId, entry: _Buffered, other: _Buffered) -> Pair:
        if incoming is StreamId.A:
            return Pair(
                msg_a=entry.message,
                msg_b=other.message,
                time_diff=entry.corrected_t - other.corrected_t,
            )
        return Pair(
            msg_a=other.message,
            msg_b=entry.message,
            time_diff=other.corrected_t - entry.corrected_t,
        )

    def finish(self) -> MatchResult:
        """结束输入, 将滞留缓存的消息记为未配对并返回结果."""
        self._finished = True
        for stream in (StreamId.A, StreamId.B):
            for entry in self._buffers[stream]:
                self._result.unmatched.append(
                    UnmatchedMessage(
                        stream=stream,
                        message=entry.message,
                        reason=UnmatchedReason.NO_MATCH_WITHIN_TOLERANCE,
                    )
                )
            self._buffers[stream] = []
        return self._result
