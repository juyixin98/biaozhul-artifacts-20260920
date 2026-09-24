"""sos_filter 模块单元测试：浮点参考与定点路径。"""

import numpy as np
import pytest

from fixedpoint_iir import FixedPointSOSFilter, QuantSpec, sos_filter_float

# 单节 Butterworth 低通（fc=1kHz, fs=8kHz, Q=1/sqrt(2)）
LP_SOS = np.array([[0.0976310729, 0.1952621458, 0.0976310729,
                    1.0, -0.9428090416, 0.3333333333]])


class TestFloatReference:
    def test_impulse_response_matches_difference_equation(self):
        # 一阶系统 y[n] = 0.5*x[n] + 0.5*y[n-1]，SOS 形式 b=[0.5,0,0], a=[1,-0.5,0]
        sos = np.array([[0.5, 0.0, 0.0, 1.0, -0.5, 0.0]])
        x = np.zeros(8)
        x[0] = 1.0
        y = sos_filter_float(sos, x)
        expected = 0.5 * 0.5 ** np.arange(8)
        np.testing.assert_allclose(y, expected, atol=1e-15)

    def test_a0_normalization_gain(self):
        # a0=2 应与归一化后等价
        sos = np.array([[0.2, 0.4, 0.2, 2.0, -1.0, 0.5]])
        sos_norm = np.array([[0.1, 0.2, 0.1, 1.0, -0.5, 0.25]])
        x = np.random.default_rng(1).uniform(-1, 1, 64)
        np.testing.assert_allclose(
            sos_filter_float(sos, x), sos_filter_float(sos_norm, x), atol=1e-12)

    def test_invalid_sos_rejected(self):
        with pytest.raises(ValueError):
            sos_filter_float(np.array([[1.0, 0, 0, 0, 0, 0]]), np.zeros(4))
        with pytest.raises(ValueError):
            sos_filter_float(np.zeros((2, 5)), np.zeros(4))


class TestFixedPointPath:
    def test_wide_word_approaches_float(self):
        # 32 位系数 / 32 位数据时，定点结果应非常接近浮点参考
        coef = QuantSpec(32, 30)
        state = QuantSpec(32, 31)
        x = np.zeros(128)
        x[0] = 0.9
        y_ref = sos_filter_float(LP_SOS, x)
        r = FixedPointSOSFilter(LP_SOS, coef, state).process(x)
        assert np.max(np.abs(r.y - y_ref)) < 1e-6
        assert r.total_overflows == 0

    def test_typical_16bit_error_bounded(self):
        coef = QuantSpec(16, 14)
        state = QuantSpec(16, 15)
        x = np.zeros(256)
        x[0] = 0.9
        y_ref = sos_filter_float(LP_SOS, x)
        r = FixedPointSOSFilter(LP_SOS, coef, state).process(x)
        assert np.max(np.abs(r.y - y_ref)) < 1e-3

    def test_large_input_saturates_and_is_counted(self):
        state = QuantSpec(16, 15)  # 范围 [-1, ~1)
        x = np.full(64, 1.5)       # 超出数据格式范围
        r = FixedPointSOSFilter(LP_SOS, QuantSpec(16, 14), state).process(x)
        assert r.n_input_overflow == 64
        assert np.max(np.abs(r.y)) <= 1.0  # 输出被饱和在格式范围内

    def test_coef_out_of_range_flagged(self):
        # a1=-2.5 超出 Q2.14 范围 [-2, 2)
        sos = np.array([[1.0, 0.0, 0.0, 1.0, -2.5, 0.0]])
        fp = FixedPointSOSFilter(sos, QuantSpec(16, 14), QuantSpec(16, 15))
        assert any(fp.coef_overflow_flags)
        # 量化后 a1 被饱和到 -2
        assert fp.sos_q[0, 4] == pytest.approx(-2.0)

    def test_state_reset_between_runs(self):
        fp = FixedPointSOSFilter(LP_SOS, QuantSpec(16, 14), QuantSpec(16, 15))
        x = np.zeros(32)
        x[0] = 1.0
        r1 = fp.process(x)
        r2 = fp.process(x)
        np.testing.assert_array_equal(r1.y_codes, r2.y_codes)

    def test_multi_section_cascade(self):
        sos2 = np.vstack([LP_SOS, LP_SOS])
        coef = QuantSpec(16, 14)
        state = QuantSpec(16, 15)
        x = np.zeros(128)
        x[0] = 0.5
        y_ref = sos_filter_float(sos2, x)
        r = FixedPointSOSFilter(sos2, coef, state).process(x)
        assert len(r.section_stats) == 2
        assert np.max(np.abs(r.y - y_ref)) < 2e-3
