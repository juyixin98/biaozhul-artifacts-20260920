"""Independent-oracle and error-budget tests.

These tests deliberately construct expectations without calling the kernel:

* exact accumulators come from the pure-Python oracle in
  :mod:`app.verification` (a separate scalar implementation);
* the float reference is a direct float64 matmul on the *original* weights;
* per-channel scales differ across channels, activation zero points are
  non-zero, and random batches cover saturation at the extrema.
"""

from __future__ import annotations

import numpy as np

from app.graph import build_model_from_float
from app.calibration import CalibrationBuilder
from app.training_state import ModelLifecycle
from app.verification import (
    per_element_error_budget,
    reference_float_layer,
    reference_integer_matmul,
    verify_trace,
)


def _build_small_model(seed: int = 7):
    """4->3->2 model with strong channel-scale spread and non-zero zps."""
    rng = np.random.default_rng(seed)
    weights = {
        "fc1": (rng.normal(0, 0.4, (3, 4))).astype(np.float32),
        "fc2": (rng.normal(0, 0.3, (2, 3))).astype(np.float32),
    }
    biases = {
        "fc1": np.array([0.3, -0.2, 0.15], np.float32),
        "fc2": np.array([-0.1, 0.25], np.float32),
    }
    x_cal = rng.normal(0, 1.0, (128, 4)).astype(np.float32)
    h_cal = x_cal @ weights["fc1"].T + biases["fc1"]
    y_cal = h_cal @ weights["fc2"].T + biases["fc2"]

    builder = CalibrationBuilder("m", "v1")
    builder.observe_input("fc1", x_cal)
    builder.observe_output("fc1", h_cal)
    builder.observe_input("fc2", h_cal)
    builder.observe_output("fc2", y_cal)
    bundle = builder.build(weights)
    return weights, biases, bundle, x_cal


def test_oracle_matches_kernel_for_random_codes() -> None:
    """Random int8 codes/non-zero zps: kernel accumulator == Python oracle."""
    rng = np.random.default_rng(11)
    m, k, n = 5, 7, 4
    qa = rng.integers(-128, 128, size=(m, k), dtype=np.int8)
    qw = rng.integers(-128, 128, size=(n, k), dtype=np.int8)
    za = 41
    zw = rng.integers(-128, 128, size=n, dtype=np.int64)
    bias_q = rng.integers(-50, 50, size=n, dtype=np.int32)

    from app.tensor_types import QuantSpec, QuantTensor, make_per_tensor_spec, make_per_channel_spec
    from app.kernel import quantized_matmul

    act = QuantTensor(qa, make_per_tensor_spec(0.03, za))
    wqt = QuantTensor(qw, make_per_channel_spec(np.linspace(0.01, 0.09, n), zw))
    out_spec = QuantSpec(np.linspace(0.005, 0.02, n), np.zeros(n, np.int64), axis=1)
    res = quantized_matmul(act, wqt, out_spec=out_spec, bias_q=bias_q)

    oracle = reference_integer_matmul(qa.tolist(), qw.tolist(), za, zw, bias_q)
    assert res.corrected.tolist() == oracle


def test_per_channel_scales_are_distinct_and_nonzero_zps() -> None:
    """The built model really uses different scales and non-zero zps."""
    weights, biases, bundle, _ = _build_small_model()
    wparams = bundle.layers["fc1"]["weight"]
    scales = wparams.scales
    assert len(scales) == 3
    assert max(scales) / min(scales) > 1.5  # channels differ meaningfully
    assert all(s > 0 for s in scales)
    assert bundle.layers["fc1"]["input"].zero_points[0] != 0


def test_full_trace_is_accepted_against_float_reference() -> None:
    weights, biases, bundle, x_cal = _build_small_model()
    model = build_model_from_float("m", "v1", weights, biases, bundle)
    x = x_cal[:6]
    trace = model.execute_float(x)
    report = verify_trace(model, x, trace, request_id="t-1")

    assert report.verdict == "ACCEPTED"
    for layer_report in report.layers:
        assert layer_report.exact_integer_match is True
        assert layer_report.violations == 0
        # Error is small in relative terms but explicitly NOT required to be 0.
        assert layer_report.max_abs_error <= layer_report.max_budget
        assert layer_report.max_abs_error >= 0.0


def test_float_and_integer_outputs_are_not_identical_but_close() -> None:
    """Quantization noise must exist somewhere; equality is never asserted."""
    weights, biases, bundle, x_cal = _build_small_model()
    model = build_model_from_float("m", "v1", weights, biases, bundle)
    x = x_cal[:16]
    trace = model.execute_float(x)
    name = model.layer_order()[-1]
    y_ref = reference_float_layer(
        trace[model.layer_order()[0]].output_float if len(trace) > 1 else x,
        weights[name], biases[name],
    )
    if len(trace) > 1:
        # fc2's input is fc1's quantized output, so reference fc2 from there.
        y_ref = reference_float_layer(
            trace["fc1"].output_float, weights["fc2"], biases["fc2"]
        )
    y_q = trace[name].output_float.astype(np.float64)
    assert y_q.shape == y_ref.shape
    assert not np.allclose(y_q, y_ref, atol=0.0)  # not bit-identical
    assert np.max(np.abs(y_q - y_ref)) > 0.0


def test_error_budget_is_data_dependent() -> None:
    """Larger code magnitudes => larger budget, deterministically."""
    qa = np.full((1, 4), 10, np.int8)
    qw = np.full((2, 4), 10, np.int8)
    small = per_element_error_budget(
        qa, qw, 0, np.zeros(2, np.int64), 0.1, np.array([0.1, 0.1]),
        np.array([0.01, 0.01]), has_bias=False,
    )
    qa_big = np.full((1, 4), 100, np.int8)
    big = per_element_error_budget(
        qa_big, qw, 0, np.zeros(2, np.int64), 0.1, np.array([0.1, 0.1]),
        np.array([0.01, 0.01]), has_bias=False,
    )
    assert np.all(big > small)


def test_corrupted_kernel_output_is_rejected() -> None:
    """Tamper with the integer accumulator -> verdict must be REJECTED."""
    weights, biases, bundle, x_cal = _build_small_model()
    model = build_model_from_float("m", "v1", weights, biases, bundle)
    x = x_cal[:3]
    trace = model.execute_float(x)

    # Mutate the stored corrected accumulator of fc1 to simulate a core bug.
    from app.graph import LayerIO
    io = trace["fc1"]
    buggy = io.integer_result.corrected.copy()
    buggy[0, 0] += 1
    tampered_result = type(io.integer_result)(
        raw=io.integer_result.raw,
        sum_x=io.integer_result.sum_x,
        sum_w=io.integer_result.sum_w,
        corrected=buggy,
        output=io.integer_result.output,
    )
    trace["fc1"] = LayerIO("fc1", tampered_result, io.output_float)

    report = verify_trace(model, x, trace, request_id="t-bug")
    assert report.verdict == "REJECTED"
    assert any("integer accumulator differs" in r for r in report.reasons)
    assert report.layers[0].exact_integer_match is False


def test_saturation_drives_undetermined_when_budget_breaks() -> None:
    """Out-of-range input saturates activations -> UNDETERMINED, never accepted.

    The analytic budget assumes per-element quantization error <= sa/2; that
    assumption is false once codes saturate, so the verifier must refuse to
    certify the result even though the integer path itself is exact.
    """
    weights, biases, bundle, _ = _build_small_model()
    model = build_model_from_float("m", "v1", weights, biases, bundle)
    x_oos = np.full((4, 4), 50.0, np.float32)  # far outside calibration range
    trace = model.execute_float(x_oos)
    report = verify_trace(model, x_oos, trace, request_id="t-oos")
    assert report.verdict == "UNDETERMINED"
    assert any(layer.violations > 0 for layer in report.layers)
    assert any("outside the" in r for r in report.reasons)


def test_frozen_calibration_cannot_be_re_estimated() -> None:
    """After freeze, the lifecycle refuses a second calibration."""
    weights, biases, bundle, _ = _build_small_model()
    lc = ModelLifecycle("m", "v1")
    lc.freeze(bundle)
    try:
        lc.freeze(bundle)
    except Exception as exc:  # noqa: BLE001
        assert "already frozen" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("second freeze must fail")


def test_bundle_fingerprint_changes_when_any_scale_changes() -> None:
    weights, biases, bundle, _ = _build_small_model()
    fp0 = bundle.fingerprint()
    from app.calibration import TensorParams
    role = bundle.layers["fc1"]["input"]
    tampered = TensorParams((role.scales[0] * 2,), role.zero_points, False, role.axis)
    layers = dict(bundle.layers)
    layers["fc1"] = dict(layers["fc1"], input=tampered)
    bundle2 = type(bundle)(
        bundle.model_id, bundle.model_version, bundle.created_at, layers
    )
    assert bundle2.fingerprint() != fp0
