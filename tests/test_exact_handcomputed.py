"""Exact element-wise integer results on a hand-computed matrix.

Every expected integer here was computed by hand (and duplicated in
``engine.numerics.exact_integer_reference``, a pure-Python big-int
reimplementation that never calls the kernel under test). The test asserts
concrete values, not "the interface ran".
"""

from __future__ import annotations

import numpy as np
import pytest

from engine.graph import as_graph_input
from engine.kernels import integer_linear
from engine.numerics import exact_integer_reference
from engine.tensor_types import QTensor
from tests.conftest import (
    EXPECTED_BIASED,
    EXPECTED_MAC,
    EXPECTED_Q_INPUT,
    EXPECTED_Q_OUT,
    EXPECTED_REAL_OUT,
    HAND_INPUT,
)

pytestmark = pytest.mark.unit


def test_encoded_input_matches_hand_values(hand_artifact) -> None:
    q = as_graph_input(QTensor(HAND_INPUT, hand_artifact.input_params)).q_values
    np.testing.assert_array_equal(q, EXPECTED_Q_INPUT)


def test_zero_point_corrected_mac_matches_hand_values(hand_artifact) -> None:
    layer = hand_artifact.layers[0]
    result = integer_linear(
        HAND_INPUT,
        layer.weight,
        layer.output_params,
        input_params=hand_artifact.input_params,
        bias_q=layer.bias_q,
        accumulator_dtype=layer.accumulator_dtype,
    )
    np.testing.assert_array_equal(result.mac_without_bias, EXPECTED_MAC)
    np.testing.assert_array_equal(result.accumulator, EXPECTED_BIASED)
    np.testing.assert_array_equal(result.q_out, EXPECTED_Q_OUT)


def test_dequantized_output_matches_hand_reals(hand_artifact) -> None:
    graph = hand_artifact.build_graph()
    from engine.tensor_types import QTensor

    out = graph.execute(
        as_graph_input(QTensor(HAND_INPUT, hand_artifact.input_params)),
        request_id="hand-exact",
    ).output
    np.testing.assert_allclose(out.values, EXPECTED_REAL_OUT, rtol=0.0, atol=0.0)
    assert out.saturated is not None
    assert not out.saturated.any()


def test_independent_bigint_oracle_agrees_elementwise(hand_artifact) -> None:
    trace = exact_integer_reference(hand_artifact, HAND_INPUT)
    layer_trace = trace.layers[0]
    assert layer_trace.q_input == EXPECTED_Q_INPUT.tolist()
    assert layer_trace.accumulator == EXPECTED_MAC.tolist()
    assert layer_trace.biased == EXPECTED_BIASED.tolist()
    assert layer_trace.q_output == EXPECTED_Q_OUT.tolist()
    assert layer_trace.real_output == EXPECTED_REAL_OUT.tolist()


def test_kernel_and_independent_oracle_agree_but_are_distinct_code(
    hand_artifact,
) -> None:
    # The oracle may read data containers (model/tensor_types) but must not
    # reuse any computational core (kernels/graph/quantize) or call the kernel.
    import ast
    import inspect

    from engine import numerics

    source = inspect.getsource(numerics)
    imported: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
    forbidden = {"engine.kernels", "engine.graph", "engine.quantize"}
    assert forbidden.isdisjoint(imported), imported
    assert "integer_linear" not in source

    layer = hand_artifact.layers[0]
    x = np.array([[0.5, -0.25], [-0.5, 0.25], [0.0, 0.0]])
    result = integer_linear(
        x,
        layer.weight,
        layer.output_params,
        input_params=hand_artifact.input_params,
        bias_q=layer.bias_q,
        accumulator_dtype=layer.accumulator_dtype,
    )
    trace = exact_integer_reference(hand_artifact, x)
    assert result.q_out.tolist() == trace.q_output


def test_relu_runs_in_integer_domain_at_nonzero_zero_point(
    hand_artifact_relu,
) -> None:
    # q_out of the linear is [-1, -3]. Channel 0 has zp=-5 (stays -1 -> 2.0),
    # channel 1 has zp=0 (clamps to exactly real 0.0).
    from engine.tensor_types import QTensor

    out = hand_artifact_relu.build_graph().execute(
        as_graph_input(QTensor(HAND_INPUT, hand_artifact_relu.input_params)),
        request_id="hand-relu",
    ).output
    np.testing.assert_allclose(out.values, [[2.0, 0.0]], atol=0.0, rtol=0.0)
    # Integer encoding of the zero output must be exactly the zero point.
    np.testing.assert_array_equal(out.q_values[0, 1], np.int8(0))


def test_bias_uses_product_scale_not_input_scale_alone() -> None:
    # b_o = 0.05, s_x = 0.25, s_w = 0.1 -> accumulator scale 0.025 -> bq = 2.
    # A common bug quantizes at s_x alone, which gives 0 (round(0.05/0.25)).
    from engine.quantize import quantize_bias

    bq = quantize_bias(np.array([0.05]), 0.25, np.array([0.1]))
    assert bq.tolist() == [2]
    bq_wrong = np.round(0.05 / 0.25).astype(np.int64)
    assert int(bq_wrong) == 0  # documents the rejected wrong answer
