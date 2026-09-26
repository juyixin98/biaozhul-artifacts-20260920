"""核心配对器测试: 对照离线定义手工演算."""

import pytest

from sensor_pairing.matcher import TimePairingMatcher
from sensor_pairing.models import SensorMessage, StreamId, UnmatchedReason


def msg(mid: str, t: float) -> SensorMessage:
    return SensorMessage(msg_id=mid, timestamp=t)


def run(matcher: TimePairingMatcher, arrivals: list[tuple[StreamId, SensorMessage]]):
    for stream, m in arrivals:
        matcher.add(stream, m)
    return matcher.finish()


class TestBasicPairing:
    def test_pair_within_tolerance(self):
        m = TimePairingMatcher(tolerance=0.05, cache_size=10)
        result = run(m, [
            (StreamId.A, msg("a1", 1.00)),
            (StreamId.B, msg("b1", 1.03)),
        ])
        assert len(result.pairs) == 1
        pair = result.pairs[0]
        assert pair.msg_a.msg_id == "a1"
        assert pair.msg_b.msg_id == "b1"
        assert pair.time_diff == pytest.approx(-0.03)
        assert result.unmatched == []

    def test_no_pair_outside_tolerance(self):
        m = TimePairingMatcher(tolerance=0.05, cache_size=10)
        result = run(m, [
            (StreamId.A, msg("a1", 1.00)),
            (StreamId.B, msg("b1", 1.10)),
        ])
        assert result.pairs == []
        assert {u.reason for u in result.unmatched} == {
            UnmatchedReason.NO_MATCH_WITHIN_TOLERANCE
        }
        assert len(result.unmatched) == 2

    def test_nearest_candidate_wins(self):
        # b1 超出容差, b2/b3 在容差内, 最近者 b3 胜出.
        m = TimePairingMatcher(tolerance=0.05, cache_size=10)
        result = run(m, [
            (StreamId.B, msg("b1", 0.90)),
            (StreamId.B, msg("b2", 0.97)),
            (StreamId.B, msg("b3", 1.02)),
            (StreamId.A, msg("a1", 1.00)),
        ])
        assert [p.msg_b.msg_id for p in result.pairs] == ["b3"]
        leftover = {u.message.msg_id for u in result.unmatched}
        assert leftover == {"b1", "b2"}


class TestSingleConsumption:
    def test_one_b_consumed_once(self):
        # 两条 A 都在 b1 容差内, 但 b1 只能配对一次(给更近的 a1).
        m = TimePairingMatcher(tolerance=0.05, cache_size=10)
        result = run(m, [
            (StreamId.B, msg("b1", 1.00)),
            (StreamId.A, msg("a1", 1.01)),
            (StreamId.A, msg("a2", 1.02)),
        ])
        assert len(result.pairs) == 1
        assert result.pairs[0].msg_a.msg_id == "a1"
        assert [u.message.msg_id for u in result.unmatched] == ["a2"]
        assert result.unmatched[0].reason == UnmatchedReason.NO_MATCH_WITHIN_TOLERANCE


class TestTieBreaking:
    def test_equal_distance_earlier_timestamp_wins(self):
        # b1/b2 与 a1 等距(0.04), 时间戳较早的 b1 胜出.
        m = TimePairingMatcher(tolerance=0.05, cache_size=10)
        result = run(m, [
            (StreamId.B, msg("b1", 0.96)),
            (StreamId.B, msg("b2", 1.04)),
            (StreamId.A, msg("a1", 1.00)),
        ])
        assert [p.msg_b.msg_id for p in result.pairs] == ["b1"]

    def test_same_timestamp_earlier_arrival_wins(self):
        # 同时间重复: b1/b2 时间戳相同, 先到达的 b1 胜出; b2 再与 a2 配对.
        m = TimePairingMatcher(tolerance=0.05, cache_size=10)
        result = run(m, [
            (StreamId.B, msg("b1", 1.00)),
            (StreamId.B, msg("b2", 1.00)),
            (StreamId.A, msg("a1", 1.00)),
            (StreamId.A, msg("a2", 1.00)),
        ])
        assert [p.msg_b.msg_id for p in result.pairs] == ["b1", "b2"]
        assert result.unmatched == []


class TestOutOfOrderArrival:
    def test_out_of_order_streams_pair_correctly(self):
        # 乱序到达: b2 先于 b1 到达, 仍按时间容差正确配对.
        m = TimePairingMatcher(tolerance=0.05, cache_size=10)
        result = run(m, [
            (StreamId.A, msg("a1", 1.00)),
            (StreamId.B, msg("b2", 2.00)),
            (StreamId.B, msg("b1", 1.01)),
            (StreamId.A, msg("a2", 2.02)),
        ])
        pairs = {(p.msg_a.msg_id, p.msg_b.msg_id) for p in result.pairs}
        assert pairs == {("a1", "b1"), ("a2", "b2")}
        assert result.unmatched == []


class TestClockOffset:
    def test_offset_correction_enables_pairing(self):
        # B 时钟领先 0.5s: 不校正无法配对, 校正后配对成功.
        arrivals = [
            (StreamId.A, msg("a1", 1.00)),
            (StreamId.B, msg("b1", 1.50)),
        ]
        uncorrected = run(TimePairingMatcher(0.05, 10), arrivals)
        assert uncorrected.pairs == []

        corrected = run(
            TimePairingMatcher(0.05, 10, clock_offset_b=0.5), arrivals
        )
        assert len(corrected.pairs) == 1
        assert corrected.pairs[0].time_diff == pytest.approx(0.0)


class TestBoundedCache:
    def test_oldest_evicted_with_reason_when_full(self):
        # cache_size=2, 第三条 A 到达时最早到达的 a1 过期挤出.
        m = TimePairingMatcher(tolerance=0.05, cache_size=2)
        result = run(m, [
            (StreamId.A, msg("a1", 1.0)),
            (StreamId.A, msg("a2", 2.0)),
            (StreamId.A, msg("a3", 3.0)),
        ])
        evicted = [
            u for u in result.unmatched
            if u.reason is UnmatchedReason.EVICTED_CACHE_FULL
        ]
        assert [u.message.msg_id for u in evicted] == ["a1"]
        flushed = [
            u for u in result.unmatched
            if u.reason is UnmatchedReason.NO_MATCH_WITHIN_TOLERANCE
        ]
        assert {u.message.msg_id for u in flushed} == {"a2", "a3"}

    def test_evicted_message_cannot_pair_later(self):
        # a1 被挤出后, 迟到的 b1 无法再与它配对.
        m = TimePairingMatcher(tolerance=0.05, cache_size=1)
        result = run(m, [
            (StreamId.A, msg("a1", 1.00)),
            (StreamId.A, msg("a2", 2.00)),  # 挤出 a1
            (StreamId.B, msg("b1", 1.01)),
        ])
        assert result.pairs == []
        reasons = {u.message.msg_id: u.reason for u in result.unmatched}
        assert reasons["a1"] is UnmatchedReason.EVICTED_CACHE_FULL
        assert reasons["b1"] is UnmatchedReason.NO_MATCH_WITHIN_TOLERANCE


class TestValidation:
    def test_negative_tolerance_rejected(self):
        with pytest.raises(ValueError):
            TimePairingMatcher(tolerance=-0.1, cache_size=10)

    def test_zero_cache_size_rejected(self):
        with pytest.raises(ValueError):
            TimePairingMatcher(tolerance=0.1, cache_size=0)

    def test_add_after_finish_rejected(self):
        m = TimePairingMatcher(0.1, 10)
        m.finish()
        with pytest.raises(RuntimeError):
            m.add(StreamId.A, msg("a1", 1.0))
