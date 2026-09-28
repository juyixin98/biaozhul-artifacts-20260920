"""Independent numeric verification.

The test/verification oracle here is deliberately **independent of the kernel
under test**:

* the exact integer reference is a pure-Python triple loop over the input
  codes — it shares no code path with :mod:`app.kernel`;
* the float reference is a straightforward float64 ``x @ W.T + b`` using the
  original unquantized weights — it never dequantizes kernel outputs.

Verdicts
--------
``ACCEPT``     – integer results match the oracle exactly *and* every element
                 is within the analytic, data-dependent quantization error
                 budget of the float reference.
``REJECTED``   – the integer result disagrees with the independent oracle
                 (a core-implementation defect, not quantization noise).
``UNDETERMINED`` – integers agree but the float budget is violated (typically
                 output saturation or an under-sized output range).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .graph import LayerIO, QuantizedModel
from .tensor_types import QMAX, QMIN


# ---------------------------------------------------------------------------
# Independent exact-integer oracle (pure Python — no kernel code reused)
# ---------------------------------------------------------------------------
def reference_integer_matmul(
    qa: np.ndarray,
    qw: np.ndarray,
    za: int,
    zw: np.ndarray,
    bias_q: np.ndarray | None,
) -> list[list[int]]:
    """Compute the corrected accumulator with an independent scalar loop.

    Pure Python ``int``s (arbitrary precision) — this cannot overflow and does
    not call any function from the module being verified.
    """
    m, k = len(qa), len(qa[0])
    n = len(qw)
    zw_list = [int(v) for v in zw]
    bias_list = [0] * n if bias_q is None else [int(v) for v in bias_q]
    sum_w = [sum(int(qw[j][t]) for t in range(k)) for j in range(n)]

    out: list[list[int]] = []
    for i in range(m):
        row = [int(v) for v in qa[i]]
        sum_x = sum(row)
        row_out: list[int] = []
        for j in range(n):
            raw = 0
            for t in range(k):
                raw += row[t] * int(qw[j][t])
            acc = (
                raw
                - zw_list[j] * sum_x
                - za * sum_w[j]
                + k * za * zw_list[j]
                + bias_list[j]
            )
            row_out.append(acc)
        out.append(row_out)
    return out


# ---------------------------------------------------------------------------
# Independent float reference
# ---------------------------------------------------------------------------
def reference_float_layer(
    x: np.ndarray,
    weight_float: np.ndarray,
    bias_float: np.ndarray | None,
) -> np.ndarray:
    """Plain float64 affine layer — the unquantized mathematical model."""
    x64 = np.asarray(x, dtype=np.float64)
    w64 = np.asarray(weight_float, dtype=np.float64)
    y = x64 @ w64.T
    if bias_float is not None:
        y = y + np.asarray(bias_float, dtype=np.float64)
    return y


# ---------------------------------------------------------------------------
# Data-dependent quantization error budget
# ---------------------------------------------------------------------------
def per_element_error_budget(
    qa: np.ndarray,
    qw: np.ndarray,
    za: int,
    zw: np.ndarray,
    sa: float,
    sw: np.ndarray,
    sy: np.ndarray,
    *,
    has_bias: bool,
) -> np.ndarray:
    """Analytic upper bound on |dequantized output - float reference|.

    Let ``da = qa - za``, ``dw = qw - zw`` (the de-quantized integer
    magnitudes) and per-element round errors ``|e_a| <= sa/2``,
    ``|e_w| <= sw/2``. Expanding ``(sa*da + e_a)(sw*dw + e_w)``::

        input error term  <= sa/2 * sw[j] * sum_k |dw[j,k]|
        weight error term <= sw/2 * sa    * sum_k |da[i,k]|
        cross term        <= K * sa * sw[j] / 4
        bias quant error  <= sa * sw[j] / 2
        output round error<= sy[j] / 2

    Saturation at the output is unboundable; saturated elements are counted
    separately and, when present, drive an UNDETERMINED verdict.
    """
    m, k = qa.shape
    sa = float(sa)
    da = np.abs(qa.astype(np.int64) - int(za))        # (M, K)
    dw = np.abs(qw.astype(np.int64) - zw.astype(np.int64)[:, np.newaxis])  # (N, K)
    sum_abs_dw = dw.sum(axis=1)                       # (N,)
    sum_abs_da = da.sum(axis=1)                       # (M,)
    sw = sw.astype(np.float64)
    sy = sy.astype(np.float64)

    input_term = 0.5 * sa * sw[np.newaxis, :] * sum_abs_dw[np.newaxis, :]
    weight_term = 0.5 * sa * sw[np.newaxis, :] * sum_abs_da[:, np.newaxis]
    cross_term = 0.25 * k * sa * sw[np.newaxis, :]
    out_term = 0.5 * sy[np.newaxis, :]
    bias_term = 0.5 * sa * sw[np.newaxis, :] if has_bias else 0.0
    # Small float32 bookkeeping floor, relative to the output scale.
    floor = 1e-6 * np.maximum(np.abs(sy[np.newaxis, :]), 1e-12)
    return input_term + weight_term + cross_term + out_term + bias_term + floor


@dataclass(frozen=True)
class LayerVerification:
    name: str
    exact_integer_match: bool
    max_abs_error: float
    max_budget: float
    violations: int
    saturated_outputs: int
    shape: tuple[int, int]

    @property
    def passed_budget(self) -> bool:
        return self.violations == 0


@dataclass(frozen=True)
class VerificationReport:
    verdict: str  # ACCEPT | REJECTED | UNDETERMINED
    request_id: str
    layers: list[LayerVerification] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "request_id": self.request_id,
            "layers": [
                {
                    "name": v.name,
                    "exact_integer_match": v.exact_integer_match,
                    "max_abs_error": v.max_abs_error,
                    "max_error_budget": v.max_budget,
                    "budget_violations": v.violations,
                    "saturated_outputs": v.saturated_outputs,
                    "shape": list(v.shape),
                }
                for v in self.layers
            ],
            "reasons": self.reasons,
        }


def verify_trace(
    model: QuantizedModel,
    x_float: np.ndarray,
    trace: "dict[str, LayerIO]",
    *,
    request_id: str,
) -> VerificationReport:
    """Run all independent checks over an executed trace.

    Redaction: only shapes, counts and aggregate error statistics are ever
    emitted — never element payloads of caller data.
    """
    layer_reports: list[LayerVerification] = []
    reasons: list[str] = []
    integer_defect = False
    budget_breach = False

    float_weights = model.float_weights
    float_biases = model.float_biases

    # Replay per-layer quantization through the *bound* input specs to recover
    # the exact activation codes each layer consumed. This exercises only
    # tensor_types (never the kernel), so the oracle stays independent.
    current = np.asarray(x_float, dtype=np.float32)
    for name, io in trace.items():
        layer = model.layers[name]
        res = io.integer_result
        act = layer.quantize_input(current)

        expected_acc = reference_integer_matmul(
            act.codes.tolist(),
            layer.weight.codes.tolist(),
            int(layer.input_spec.zero_point),
            np.asarray(layer.weight.spec.zero_point, dtype=np.int64),
            None if layer.bias_q is None else np.asarray(layer.bias_q),
        )
        actual_acc = res.corrected.tolist()
        exact_match = expected_acc == actual_acc
        if not exact_match:
            integer_defect = True
            diffs = [
                (i, j, expected_acc[i][j], actual_acc[i][j])
                for i in range(len(expected_acc))
                for j in range(len(expected_acc[i]))
                if expected_acc[i][j] != actual_acc[i][j]
            ][:3]
            reasons.append(
                f"layer {name!r}: integer accumulator differs from independent "
                f"oracle at {len(diffs)}+ location(s), first={diffs}"
            )

        # --- check 2: float reference vs dequantized output ------------------
        y_ref = reference_float_layer(
            current, float_weights[name], float_biases[name]
        )
        y_q = io.output_float.astype(np.float64)
        budget = per_element_error_budget(
            act.codes,
            layer.weight.codes,
            int(layer.input_spec.zero_point),
            np.asarray(layer.weight.spec.zero_point, dtype=np.int64),
            float(layer.input_spec.scale),
            np.asarray(layer.weight.spec.scale, dtype=np.float64),
            np.asarray(layer.output_spec.scale, dtype=np.float64),
            has_bias=layer.bias_q is not None,
        )
        abs_err = np.abs(y_q - y_ref)
        violations = int(np.count_nonzero(abs_err > budget))
        saturated = int(np.count_nonzero(
            (res.output.codes <= QMIN) | (res.output.codes >= QMAX)
        ))
        layer_reports.append(
            LayerVerification(
                name=name,
                exact_integer_match=exact_match,
                max_abs_error=float(np.max(abs_err)),
                max_budget=float(np.max(budget)),
                violations=violations,
                saturated_outputs=saturated,
                shape=tuple(y_q.shape),
            )
        )
        if violations:
            budget_breach = True
            reasons.append(
                f"layer {name!r}: {violations} element(s) outside the "
                f"quantization error budget (max_err={float(np.max(abs_err)):.6g}, "
                f"max_budget={float(np.max(budget)):.6g}, saturated={saturated})"
            )
        current = io.output_float

    if integer_defect:
        verdict = "REJECTED"
    elif budget_breach:
        verdict = "UNDETERMINED"
    else:
        verdict = "ACCEPTED"
    if not reasons:
        reasons.append("all layers: integers exact, float errors within budget")
    return VerificationReport(
        verdict=verdict, request_id=request_id,
        layers=layer_reports, reasons=reasons,
    )
