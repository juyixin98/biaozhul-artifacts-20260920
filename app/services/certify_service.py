"""Service orchestration: request validation, certification, serialisation.

This is the single boundary where raw input becomes validated internal data
and where internal mpmath results become JSON-safe dictionaries. The FastAPI
router stays thin and delegates here.
"""

from __future__ import annotations

import mpmath

from ..core.approximation import approximate_roots
from ..core.certifier import (
    Certifier,
    CertificationResult,
)
from ..core.config import CertConfig
from ..core.derivative import differentiate
from ..core.errors import (
    ErrorCode,
    InvalidRequest,
    StateConflict,
)
from ..core.evaluator import Evaluator
from ..core.interval_utils import IntervalOps
from ..core.parser import parse_expression
from ..core.tracer import Tracer

# A conservative ceiling on the user search interval width; wider runs are
# allowed but must be explicit (kept modest for the local service).
DEFAULT_SAMPLE_COUNT = 1001
MAX_SAMPLE_COUNT = 20_001


def certify_expression(
    expression: str,
    lower: str,
    upper: str,
    config: CertConfig,
    tracer: Tracer,
    include_approximation: bool = True,
    sample_count: int = DEFAULT_SAMPLE_COUNT,
) -> dict:
    """Validate input, run certification, and build the response payload."""
    _validate_scalars(lower, upper, sample_count)
    if len(expression) > config.max_expression_len:
        raise InvalidRequest(
            f"expression length {len(expression)} exceeds limit "
            f"{config.max_expression_len}",
            code=ErrorCode.EXPRESSION_TOO_LARGE,
            details={
                "length": len(expression),
                "max_length": config.max_expression_len,
            },
        )

    f_ast = parse_expression(expression)  # raises ParseError (positioned)
    fp_ast = differentiate(f_ast)

    ev = Evaluator(config.precision_dps)
    io = IntervalOps(ev)
    try:
        lo = ev.mp.mpf(lower)
        hi = ev.mp.mpf(upper)
    except (ValueError, TypeError) as exc:
        raise InvalidRequest(
            f"interval bounds must be decimal numbers: {exc}",
        ) from exc

    if lo >= hi:
        raise StateConflict(
            "lower bound must be strictly less than upper bound",
            {"lower": lower, "upper": upper},
        )
    if not (mpmath.isfinite(lo) and mpmath.isfinite(hi)):
        raise InvalidRequest("interval bounds must be finite")

    tracer.event(
        "request", "accepted",
        expression=expression,
        lower=str(lo),
        upper=str(hi),
        precision_dps=config.precision_dps,
        target_width=config.target_width,
    )

    certifier = Certifier(f_ast, fp_ast, ev, config, tracer)
    result = certifier.run(lo, hi)

    approximation = None
    if include_approximation:
        approximation = approximate_roots(
            f_ast, ev, lo, hi, config, sample_count=sample_count
        )

    payload = _serialize(
        expression, lo, hi, result, io, config, approximation
    )
    return payload


def _validate_scalars(lower: str, upper: str, sample_count: int) -> None:
    if not isinstance(lower, str) or not isinstance(upper, str):
        raise InvalidRequest("interval bounds must be provided as strings")
    if not lower.strip() or not upper.strip():
        raise InvalidRequest("interval bounds must not be empty")
    if sample_count < 3 or sample_count > MAX_SAMPLE_COUNT:
        raise InvalidRequest(
            f"sample_count must be in [3, {MAX_SAMPLE_COUNT}]",
        )


def _serialize(
    expression: str,
    lo,
    hi,
    result: CertificationResult,
    io: IntervalOps,
    config: CertConfig,
    approximation,
) -> dict:
    digits = config.display_digits

    certified = [
        _serialize_root(root, io, digits) for root in result.roots
    ]
    undecided = [
        _serialize_undecided(region, io, digits) for region in result.undecided
    ]

    status = _overall_status(result)

    payload: dict = {
        "expression": expression,
        "search_interval": {
            "lower": io.decimal_point(lo, digits),
            "upper": io.decimal_point(hi, digits),
        },
        "status": status,
        "certified_roots": certified,
        "undecided_regions": undecided,
        "summary": {
            "certified_root_count": len(certified),
            "undecided_region_count": len(undecided),
            "root_free": result.root_free,
            "excluded_leaves": result.excluded_leaves,
            "bisections": result.bisections,
            "newton_steps": result.newton_steps,
            "value_evaluations": result.value_evaluations,
            "derivative_evaluations": result.derivative_evaluations,
            "budget_hit": result.budget_hit,
        },
    }
    if approximation is not None:
        payload["approximate_roots_unverified"] = _serialize_approximation(
            approximation
        )
    return payload


def _overall_status(result: CertificationResult) -> str:
    """Classify the whole run.

    * ``certified``           - theorem-backed roots found and nothing pending
    * ``root_free``           - whole interval rigorously excludes roots
    * ``partially_certified`` - some roots certified, regions still pending
    * ``undecided``           - no roots certified and pending regions remain
    """
    if result.budget_hit is not None:
        return "partially_certified" if result.roots else "undecided"
    if result.roots:
        return "certified"
    if result.root_free:
        return "root_free"
    return "undecided"


def _serialize_root(root, io: IntervalOps, digits: int) -> dict:
    lo_s, hi_s = io.outward_decimal(io.make(root.lo, root.hi), digits)
    res_lo, res_hi = root.residual_range
    d_lo, d_hi = root.derivative_range
    return {
        "enclosure": {"lower": lo_s, "upper": hi_s},
        "midpoint": io.decimal_point(root.midpoint, digits),
        "certification": {
            "theorem": root.theorem,
            "status": "certified",
            "newton_iterations": root.newton_iterations,
        },
        "evidence": root.evidence,
        "residual_range_enclosure": {
            "lower": io.decimal_point(res_lo, digits),
            "upper": io.decimal_point(res_hi, digits),
        },
        "derivative_range_enclosure": {
            "lower": io.decimal_point(d_lo, digits),
            "upper": io.decimal_point(d_hi, digits),
        },
    }


def _serialize_undecided(region, io: IntervalOps, digits: int) -> dict:
    lo_s, hi_s = io.outward_decimal(io.make(region.lo, region.hi), digits)
    v_lo, v_hi = region.value_range
    out: dict = {
        "interval": {"lower": lo_s, "upper": hi_s},
        "reason": region.reason,
        "depth": region.depth,
        "value_range_enclosure": {
            "lower": io.decimal_point(v_lo, digits),
            "upper": io.decimal_point(v_hi, digits),
        },
    }
    if region.derivative_range is not None:
        d_lo, d_hi = region.derivative_range
        out["derivative_range_enclosure"] = {
            "lower": io.decimal_point(d_lo, digits),
            "upper": io.decimal_point(d_hi, digits),
        }
    return out


def _serialize_approximation(report) -> dict:
    return {
        "status": "approximate_unverified",
        "warning": (
            "Floating-point candidates from SciPy Brent search; these are NOT "
            "certified and must not be treated as proof. Compare against "
            "certified_roots / undecided_regions."
        ),
        "sample_count": report.sample_count,
        "convergence_failures": report.convergence_failures,
        "roots": [
            {
                "value": f"{root.value:.15g}",
                "residual": f"{root.residual:.3e}",
                "method": root.method,
            }
            for root in report.roots
        ],
    }
