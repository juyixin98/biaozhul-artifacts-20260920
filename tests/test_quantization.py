"""quantization 模块单元测试。"""

import numpy as np
import pytest

from fixedpoint_iir.quantization import (
    QuantSpec,
    dequantize_array,
    quantize_array,
    quantize_scalar,
    round_accumulator_to_spec,
)


class TestQuantSpec:
    def test_q2_14_properties(self):
        spec = QuantSpec(16, 14)
        assert spec.int_bits == 2
        assert spec.qmin == -32768
        assert spec.qmax == 32767
        assert spec.min_value == pytest.approx(-2.0)
        assert spec.max_value == pytest.approx(32767 / 16384)
        assert spec.resolution == pytest.approx(1 / 16384)

    def test_invalid_spec_rejected(self):
        with pytest.raises(ValueError):
            QuantSpec(1, 0)
        with pytest.raises(ValueError):
            QuantSpec(16, 16)
        with pytest.raises(ValueError):
            QuantSpec(16, 14, rounding="nearest")
        with pytest.raises(ValueError):
            QuantSpec(16, 14, overflow="clamp")


class TestRoundingModes:
    def test_trunc_toward_zero(self):
        spec = QuantSpec(8, 4, rounding="trunc")
        code, _ = quantize_scalar(0.09375 * 1.5, spec)  # 1.40625 lsb -> 1
        assert code == int(np.trunc(0.140625 * 16))

    def test_round_half_away_from_zero(self):
        spec = QuantSpec(8, 4, rounding="round")
        # 2.5 lsb -> 3；-2.5 lsb -> -3
        assert quantize_scalar(2.5 / 16, spec)[0] == 3
        assert quantize_scalar(-2.5 / 16, spec)[0] == -3

    def test_convergent_bankers(self):
        spec = QuantSpec(8, 4, rounding="convergent")
        # 2.5 lsb -> 2（取偶）；3.5 lsb -> 4
        assert quantize_scalar(2.5 / 16, spec)[0] == 2
        assert quantize_scalar(3.5 / 16, spec)[0] == 4


class TestOverflow:
    def test_saturate_clips_and_counts(self):
        spec = QuantSpec(8, 6, overflow="saturate")  # max 127/64
        codes, n_over = quantize_array(np.array([1.0, 1.984375, 5.0, -5.0]), spec)
        assert n_over == 2
        assert codes[2] == 127
        assert codes[3] == -128
        assert codes[0] == 64

    def test_wrap_around(self):
        spec = QuantSpec(8, 6, overflow="wrap")
        # 128 -> 回绕为 -128
        code, ov = quantize_scalar(2.0, spec)
        assert ov
        assert code == -128


class TestAccumulatorRounding:
    def test_shift_and_round(self):
        spec = QuantSpec(16, 15, rounding="round")
        # acc 值 3，右移 1 位 = 1.5 -> 2
        codes, _ = round_accumulator_to_spec(np.array([3]), 1, spec)
        assert codes[0] == 2

    def test_trunc_shift(self):
        spec = QuantSpec(16, 15, rounding="trunc")
        codes, _ = round_accumulator_to_spec(np.array([3, -3]), 1, spec)
        assert codes[0] == 1
        assert codes[1] == -1

    def test_accumulator_saturation(self):
        spec = QuantSpec(4, 2, rounding="round", overflow="saturate")
        codes, n_over = round_accumulator_to_spec(np.array([1 << 20]), 2, spec)
        assert codes[0] == spec.qmax
        assert n_over == 1

    def test_negative_shift_rejected(self):
        with pytest.raises(ValueError):
            round_accumulator_to_spec(np.array([1]), -1, QuantSpec(16, 15))


class TestRoundTrip:
    def test_dequantize_inverse(self):
        spec = QuantSpec(16, 14)
        values = np.linspace(-1.9, 1.9, 50)
        codes, _ = quantize_array(values, spec)
        back = dequantize_array(codes, spec)
        assert np.max(np.abs(back - values)) <= spec.resolution
