"""时钟偏移校正与估计测试."""

import numpy as np
import pytest

from sensor_pairing.clock import (
    apply_clock_offset,
    estimate_clock_offset,
    estimate_offset_from_data,
)
from sensor_pairing.models import SensorMessage
from sensor_pairing.synthetic import generate_synthetic_streams


def _positions(stream):
    return [[m.data["x"], m.data["y"]] for m in stream]


class TestApplyClockOffset:
    def test_returns_new_messages_with_shifted_time(self):
        original = [SensorMessage("b1", 1.5, {"v": 1}), SensorMessage("b2", 2.5)]
        shifted = apply_clock_offset(original, 0.5)
        assert [m.timestamp for m in shifted] == [1.0, 2.0]
        # 原列表不被修改(不可变)
        assert [m.timestamp for m in original] == [1.5, 2.5]
        assert shifted[0].data == {"v": 1}


class TestEstimateClockOffset:
    def test_recovers_small_offset_from_synthetic_streams(self):
        # 纯时间戳估计适用于两路采样同一事件序列(同频率、时钟平移).
        streams = generate_synthetic_streams(
            duration=10.0,
            rate_a_hz=30.0,
            rate_b_hz=30.0,
            clock_offset_b=0.01,
            time_jitter_std=0.001,
            seed=7,
        )
        offset = estimate_clock_offset(
            [m.timestamp for m in streams.stream_a],
            [m.timestamp for m in streams.stream_b],
        )
        assert offset == pytest.approx(0.01, abs=0.005)

    def test_zero_offset(self):
        a = np.arange(0.0, 5.0, 0.02)
        b = np.arange(0.0, 5.0, 0.033)
        assert estimate_clock_offset(a, b) == pytest.approx(0.0, abs=1e-9)

    def test_max_diff_filters_outliers(self):
        a = [1.0, 2.0, 3.0]
        b = [1.1, 2.1, 100.0]  # 100 是离群点
        offset = estimate_clock_offset(a, b, max_diff=0.5)
        assert offset == pytest.approx(0.1, abs=1e-9)

    def test_empty_input_rejected(self):
        with pytest.raises(ValueError):
            estimate_clock_offset([], [1.0])
        with pytest.raises(ValueError):
            estimate_clock_offset([1.0], [])

    def test_max_diff_without_samples_rejected(self):
        with pytest.raises(ValueError):
            estimate_clock_offset([1.0], [5.0], max_diff=0.1)


class TestEstimateOffsetFromData:
    def test_recovers_large_offset_beyond_sampling_period(self):
        # 0.1s 偏移 > 半个 B 采样周期, 纯时间戳法会混叠, 数据辅助法可恢复.
        streams = generate_synthetic_streams(
            duration=10.0,
            clock_offset_b=0.1,
            noise_std=0.005,
            time_jitter_std=0.0005,
            seed=21,
        )
        offset = estimate_offset_from_data(
            [m.timestamp for m in streams.stream_a],
            _positions(streams.stream_a),
            [m.timestamp for m in streams.stream_b],
            _positions(streams.stream_b),
        )
        assert offset == pytest.approx(0.1, abs=0.01)

    def test_shape_mismatch_rejected(self):
        with pytest.raises(ValueError):
            estimate_offset_from_data([1.0, 2.0], [[0.0, 0.0]], [1.0], [[0.0, 0.0]])
