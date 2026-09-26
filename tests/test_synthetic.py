"""合成数据生成与端到端配对测试(乱序、不同频率、时钟偏移)."""

import pytest

from sensor_pairing.clock import estimate_offset_from_data
from sensor_pairing.matcher import TimePairingMatcher
from sensor_pairing.models import StreamId, UnmatchedReason
from sensor_pairing.synthetic import (
    generate_synthetic_streams,
    interleave_by_arrival,
)


class TestSyntheticGeneration:
    def test_reproducible_with_seed(self):
        s1 = generate_synthetic_streams(seed=3)
        s2 = generate_synthetic_streams(seed=3)
        assert [m.timestamp for m in s1.stream_a] == [
            m.timestamp for m in s2.stream_a
        ]

    def test_disorder_window_preserves_multiset(self):
        ordered = generate_synthetic_streams(disorder_window=1, seed=5)
        shuffled = generate_synthetic_streams(disorder_window=8, seed=5)
        # 打乱只改变到达顺序, 消息集合不变(数量一致).
        assert len(ordered.stream_a) == len(shuffled.stream_a)
        assert len(ordered.stream_b) == len(shuffled.stream_b)

    def test_interleave_preserves_per_stream_order(self):
        streams = generate_synthetic_streams(
            duration=2.0, disorder_window=4, seed=11
        )
        merged = interleave_by_arrival(streams.stream_a, streams.stream_b, seed=1)
        assert [m for s, m in merged if s == "a"] == streams.stream_a
        assert [m for s, m in merged if s == "b"] == streams.stream_b


class TestEndToEndSynthetic:
    def test_different_frequencies_all_slower_stream_paired(self):
        # 50Hz vs 30Hz: 每条低频消息都应找到容差内的高频配对.
        streams = generate_synthetic_streams(
            duration=10.0,
            rate_a_hz=50.0,
            rate_b_hz=30.0,
            time_jitter_std=0.001,
            disorder_window=3,
            seed=42,
        )
        matcher = TimePairingMatcher(tolerance=0.02, cache_size=64)
        for stream, m in interleave_by_arrival(
            streams.stream_a, streams.stream_b, seed=2
        ):
            matcher.add(StreamId(stream), m)
        result = matcher.finish()
        assert len(result.pairs) == len(streams.stream_b)
        assert len(result.unmatched_for(StreamId.B)) == 0
        # 高频流多余的消息在 finish 时记为未配对.
        assert len(result.unmatched_for(StreamId.A)) == (
            len(streams.stream_a) - len(streams.stream_b)
        )

    def test_clock_offset_estimated_then_corrected(self):
        # 带 0.1s 时钟偏移: 数据辅助估计偏移, 校正后配对.
        streams = generate_synthetic_streams(
            duration=10.0,
            clock_offset_b=0.1,
            noise_std=0.005,
            time_jitter_std=0.0005,
            disorder_window=3,
            seed=9,
        )
        offset = estimate_offset_from_data(
            [m.timestamp for m in streams.stream_a],
            [[m.data["x"], m.data["y"]] for m in streams.stream_a],
            [m.timestamp for m in streams.stream_b],
            [[m.data["x"], m.data["y"]] for m in streams.stream_b],
        )
        assert offset == pytest.approx(0.1, abs=0.01)
        matcher = TimePairingMatcher(
            tolerance=0.02, cache_size=64, clock_offset_b=offset
        )
        for stream, m in interleave_by_arrival(
            streams.stream_a, streams.stream_b, seed=4
        ):
            matcher.add(StreamId(stream), m)
        result = matcher.finish()
        # 贪心单次消费在流尾可能有少量 B 失配, 允许极小尾差.
        assert len(result.pairs) >= len(streams.stream_b) - 3
        assert len(result.unmatched_for(StreamId.B)) <= 3

    def test_tiny_cache_expires_messages(self):
        # 极小缓存下大量消息因过期被挤出, 原因可统计.
        streams = generate_synthetic_streams(
            duration=5.0, disorder_window=5, seed=13
        )
        matcher = TimePairingMatcher(tolerance=0.02, cache_size=2)
        for stream, m in interleave_by_arrival(
            streams.stream_a, streams.stream_b, seed=6
        ):
            matcher.add(StreamId(stream), m)
        result = matcher.finish()
        total = len(streams.stream_a) + len(streams.stream_b)
        # 每对消费两条消息, 未配对各计一条, 总数守恒.
        assert 2 * len(result.pairs) + len(result.unmatched) == total
        evicted = [
            u
            for u in result.unmatched
            if u.reason is UnmatchedReason.EVICTED_CACHE_FULL
        ]
        assert len(evicted) > 0
