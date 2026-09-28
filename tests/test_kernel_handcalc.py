"""Hand-computed integer tests for the fixed-point kernel.

Every expected number below is computed with pen-and-paper arithmetic in the
docstrings/comments — neither via the kernel nor via its oracle. The
independent pure-Python oracle is cross-checked in a separate module, so a
shared implementation bug cannot manufacture passing expectations here.

Hand case (M=1, K=2, N=2), deliberately chosen to exercise:

* non-zero activation zero point (za=3);
* *different*, non-zero weight zero points per channel (zw=-2 and zw=10);
* different per-channel weight scales (0.5 and 0.25);
* output multipliers of exactly 1 plus a non-zero output zp on channel 1;
* bias expressed in the accumulator scale sa*sw[j].

Real operands::

    sa=0.25, za=3;  qa=[2, 5]  ->  a=[-0.25, 0.5]
    ch0: sw=0.5, zw=-2,  qw=[1, -4] -> w=[1.5, -1.0]
    ch1: sw=0.25, zw=10, qw=[12, 0] -> w=[0.5, -2.5]
    out ch0: sy=0.125, zy=0 ; ch1: sy=0.0625, zy=-5  (mul == 1 both)
    bias_q=[8, -16]  i.e. real bias [+1.0, -1.0]
"""

from __future__ import annotations

import numpy as np
import pytest

from app.kernel import (
    ACC_MAX,
    AccumulatorOverflow,
    quantize_bias,
    quantized_matmul,
    round_half_away,
)
from app.tensor_types import (
    QMAX,
    QMIN,
    QuantSpec,
    QuantTensor,
    make_per_channel_spec,
    make_per_tensor_spec,
)


def _hand_operands(out_zy=(0, -5)):
    act_spec = make_per_tensor_spec(0.25, 3)
    weight_spec = make_per_channel_spec([0.5, 0.25], [-2, 10])
    out_spec = QuantSpec(
        scale=np.asarray([0.125, 0.0625], dtype=np.float64),
        zero_point=np.asarray(out_zy, dtype=np.int64),
        axis=1,
    )
    act = QuantTensor(np.array([[2, 5]], dtype=np.int8), act_spec)
    weight = QuantTensor(np.array([[1, -4], [12, 0]], dtype=np.int8), weight_spec)
    return act, weight, out_spec


def test_handcalc_raw_dot_products() -> None:
    """raw = sum_k qa*qw, values: 2*1+5*(-4)=-18 and 2*12+5*0=24."""
    act, weight, out_spec = _hand_operands()
    res = quantized_matmul(act, weight, out_spec=out_spec)
    assert res.raw.tolist() == [[-18, 24]]
    assert res.sum_x.tolist() == [7]
    assert res.sum_w.tolist() == [-3, 12]


def test_handcalc_zero_point_correction_without_bias() -> None:
    """acc[j] = raw - zw*sumx - za*sumw + K*za*zw:

    ch0: -18 -(-2)*7 -3*(-3) + 2*3*(-2) = -18+14+9-12 = -7
    ch1:  24 - 10*7  -3*12   + 2*3*10   =  24-70-36+60 = -22
    """
    act, weight, out_spec = _hand_operands()
    res = quantized_matmul(act, weight, out_spec=out_spec)
    assert res.corrected.tolist() == [[-7, -22]]


def test_handcalc_bias_in_accumulator_scale() -> None:
    """bias [+1.0, -1.0] at scales [0.125, 0.0625] -> codes [8, -16]."""
    act_spec = make_per_tensor_spec(0.25, 3)
    weight_spec = make_per_channel_spec([0.5, 0.25], [-2, 10])
    bias_q = quantize_bias(np.array([1.0, -1.0]), act_spec, weight_spec)
    assert bias_q.tolist() == [8, -16]

    act, weight, out_spec = _hand_operands()
    res = quantized_matmul(act, weight, out_spec=out_spec, bias_q=bias_q)
    assert res.corrected.tolist() == [[1, -38]]


def test_handcalc_output_codes_and_dequant() -> None:
    """With bias, mul=1: yq0 = 1+0 = 1, yq1 = -38+(-5) = -43.

    Dequantized: ch0 0.125*(1-0)=0.125 ; ch1 0.0625*(-43+5)=-2.375 —
    both equal the float model exactly in this hand case.
    """
    act_spec = make_per_tensor_spec(0.25, 3)
    weight_spec = make_per_channel_spec([0.5, 0.25], [-2, 10])
    bias_q = quantize_bias(np.array([1.0, -1.0]), act_spec, weight_spec)
    act, weight, out_spec = _hand_operands()
    res = quantized_matmul(act, weight, out_spec=out_spec, bias_q=bias_q)

    assert res.output.codes.dtype == np.int8
    assert res.output.codes.tolist() == [[1, -43]]
    np.testing.assert_allclose(
        res.output.dequantize(), [[0.125, -2.375]], rtol=0, atol=1e-7
    )


def test_round_half_away_is_deterministic() -> None:
    """Explicit rounding vectors — never the host C library's current mode."""
    vals = np.array([0.5, -0.5, 1.5, 2.5, -2.5, 0.4999, -0.4999])
    out = round_half_away(vals)
    assert out.tolist() == [1, -1, 2, 3, -3, 0, 0]


def test_scaling_before_round_then_zp_then_saturate_order() -> None:
    """mul=2, zy=10: yq = saturate(round(acc*2) + 10), not round(acc*2+10).

    acc=-7: round(-14)+10 = -4. Folding zp into the float product would
    round at a different stage and is a distinct convention.
    """
    act, weight, _ = _hand_operands()
    out_spec = QuantSpec(
        scale=np.asarray([0.0625, 0.03125], dtype=np.float64),  # mul = 2
        zero_point=np.asarray([10, 10], dtype=np.int64),
        axis=1,
    )
    res = quantized_matmul(act, weight, out_spec=out_spec)
    # acc = [-7, -22] -> round([-14, -44]) + 10 = [-4, -34]
    assert res.output.codes.tolist() == [[-4, -34]]


def test_saturation_happens_at_both_bounds() -> None:
    """zy=127 pins the positive side to 127; zy=-128 pins negative to -128."""
    act, weight, _ = _hand_operands()
    hi = QuantSpec(np.asarray([0.125, 0.0625]), np.asarray([127, 127]), axis=1)
    lo = QuantSpec(np.asarray([0.125, 0.0625]), np.asarray([-128, -128]), axis=1)
    res_hi = quantized_matmul(act, weight, out_spec=hi)
    res_lo = quantized_matmul(act, weight, out_spec=lo)
    assert res_hi.output.codes.tolist() == [[120, 105]]   # -7+127, -22+127
    assert res_lo.output.codes.tolist() == [[-128, -128]]  # clamp both
    assert int(np.count_nonzero(res_lo.output.codes == -128)) == 2


def test_extreme_codes_nonzero_zero_points_match_real_math() -> None:
    """Codes at -128/127 with za=127, zw=[-128, 127] still match real math.

    Channel 0 (K=1):
        a = 0.01*(-128-127) = -2.55
        w = 0.02*(-128-(-128)) = 0.0  -> dot 0
    Channel 1:
        w = 0.5*(127-127) = 0 -> dot 0
    Bias 2.04 on ch0 at scale sa*sw=0.0002 -> code 10200; output mul=1,
    zy=0 -> yq=10200 saturates to 127 (positive extreme path).
    """
    act_spec = make_per_tensor_spec(0.01, 127)
    weight_spec = make_per_channel_spec([0.02, 0.5], [-128, 127])
    out_spec = QuantSpec(np.asarray([0.0002, 0.005]), np.asarray([0, 0]), axis=1)
    act = QuantTensor(np.array([[-128]], dtype=np.int8), act_spec)
    weight = QuantTensor(np.array([[-128], [127]], dtype=np.int8), weight_spec)
    bias_q = quantize_bias(np.array([2.04, 0.0]), act_spec, weight_spec)
    assert bias_q.tolist() == [10200, 0]

    res = quantized_matmul(act, weight, out_spec=out_spec, bias_q=bias_q)
    # corrected accumulators are 10200 and 0; first saturates to 127.
    assert res.output.codes.tolist() == [[127, 0]]
    dequant = res.output.dequantize()
    np.testing.assert_allclose(dequant[0, 1], 0.0, atol=1e-7)


def test_int32_accumulator_overflow_is_an_explicit_error() -> None:
    """K=200_000, qa=qw=-128, za=zw=0: raw = K*16384 = 3.2768e9 > int32 max.

    The int64 workspace observes the crossing and raises — no host wraparound.
    """
    k = 200_000
    act_spec = make_per_tensor_spec(1.0, 0)
    weight_spec = make_per_channel_spec([1.0], [0])
    out_spec = QuantSpec(np.asarray([1.0]), np.asarray([0]), axis=1)
    act = QuantTensor(np.full((1, k), -128, dtype=np.int8), act_spec)
    weight = QuantTensor(np.full((1, k), -128, dtype=np.int8), weight_spec)
    with pytest.raises(AccumulatorOverflow, match="accumulator"):
        quantized_matmul(act, weight, out_spec=out_spec)


def test_bias_outside_int32_envelope_rejected() -> None:
    act_spec = make_per_tensor_spec(1.0, 0)
    weight_spec = make_per_channel_spec([1.0], [0])
    with pytest.raises(AccumulatorOverflow, match="bias"):
        quantize_bias(np.array([3e12]), act_spec, weight_spec)


def test_storage_bounds_constants() -> None:
    assert QMIN == -128 and QMAX == 127
    assert ACC_MAX == 2_147_483_647
