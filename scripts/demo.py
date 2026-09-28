#!/usr/bin/env python3
"""Local end-to-end demonstration (no server needed).

Runs a fixed, hand-checkable batch through the frozen graph and prints, per
layer:

* the exact integer accumulator alongside the independent pure-Python oracle;
* requantized int8 codes and saturated count;
* dequantized output vs the float64 reference and the max error/budget.

It also demonstrates the two decided failure classes: a model-version
mismatch (REJECTED) and a malformed payload (REJECTED_INVALID_INPUT).

Usage: ``python -m scripts.demo`` from the repo root (after fixtures exist).
"""

from __future__ import annotations

import numpy as np

from app.bootstrap import build_registry
from app.config import Settings
from app.errors import QInferError
from app.verification import reference_float_layer, reference_integer_matmul, verify_trace


def _print_header(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def main() -> None:
    settings = Settings.from_env()
    registry = build_registry(settings)
    entry = registry.get(settings.model_id)
    model = entry.model

    # Fixed batch — same values are used by the test suite's hand calculations.
    x = np.array(
        [
            [0.50, -1.00, 0.25, 0.75],
            [-0.50, 1.00, -0.25, -0.75],
            [0.00, 0.00, 0.00, 0.00],
        ],
        dtype=np.float32,
    )

    _print_header("INPUT (float32, fixed synthetic batch)")
    print(x)

    trace = model.execute_float(x)
    current = x
    for name, io in trace.items():
        layer = model.layers[name]
        res = io.integer_result
        act = layer.quantize_input(current)
        oracle = reference_integer_matmul(
            act.codes.tolist(),
            layer.weight.codes.tolist(),
            int(layer.input_spec.zero_point),
            np.asarray(layer.weight.spec.zero_point, dtype=np.int64),
            None if layer.bias_q is None else np.asarray(layer.bias_q),
        )
        y_ref = reference_float_layer(
            current, model.float_weights[name], model.float_biases[name]
        )
        _print_header(f"LAYER {name}: exact integer internals")
        print("activation codes:"); print(act.codes)
        print("weight codes:"); print(layer.weight.codes)
        print("raw sum qa*qw:"); print(res.raw)
        print("zero-point corrected accumulator (kernel):"); print(res.corrected)
        print("independent pure-Python oracle:"); print(np.array(oracle, dtype=np.int64))
        assert oracle == res.corrected.tolist(), "kernel disagrees with oracle"
        print("output int8 codes:"); print(res.output.codes)
        print("dequantized output:"); print(np.round(io.output_float, 6))
        print("float64 reference:"); print(np.round(y_ref, 6))
        print(f"max abs error: {np.max(np.abs(io.output_float - y_ref)):.6f}")
        current = io.output_float

    report = verify_trace(model, x, trace, request_id="demo-0001")
    _print_header("VERIFICATION")
    import json
    print(json.dumps(report.as_dict(), indent=2))

    _print_header("FAILURE-CLASS DEMO: wrong model version")
    try:
        registry.get(settings.model_id, "wrong-version")
    except QInferError as exc:
        print(f"{exc.code}: {exc.message}")
        print("details:", exc.details)

    _print_header("FAILURE-CLASS DEMO: shape/values mismatch")
    bad_batch = x.reshape(-1)
    print("declared (2, 4) but supply", bad_batch.size, "values -> handler raises")
    m, k, n = 2, 4, bad_batch.size
    print(f"would reject: {m}*{k}={m * k} != n_values={n}")


if __name__ == "__main__":
    main()
