"""Unit tests for differentiation rules, both evaluation paths, and intervals."""

from __future__ import annotations

import mpmath as mp
import pytest

from app.core.derivative import differentiate
from app.core.evaluator import Evaluator
from app.core.interval_utils import IntervalOps
from app.core.parser import parse_expression


@pytest.fixture
def ev():
    return Evaluator(50)


@pytest.fixture
def io(ev):
    return IntervalOps(ev)


def _mpi(io, a, b):
    return io.make(str(a), str(b))


# ---------------------------------------------------------------------------
# Differentiation rules (symbolic correctness via independent finite diff)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "expr,point",
    [
        ("sin(x)", "0.7"),
        ("cos(x)", "0.4"),
        ("tan(x)", "0.3"),
        ("exp(x)", "0.6"),
        ("log(x)", "1.3"),
        ("sqrt(x)", "2.2"),
        ("x/(x^2+1)", "1.1"),
        ("x^4", "0.9"),
        ("x^-2", "1.5"),
        ("sin(x)*cos(x)", "0.5"),
    ],
)
def test_derivative_matches_finite_difference(ev, expr, point) -> None:
    ast = parse_expression(expr)
    dast = differentiate(ast)
    p = ev.mp.mpf(point)
    h = ev.mp.mpf("1e-20")
    fp = ev.point_mp(ast, p)
    fph = ev.point_mp(ast, p + h)
    fmh = ev.point_mp(ast, p - h)
    finite_diff = (fph - fmh) / (2 * h)
    symbolic = ev.point_mp(dast, p)
    assert abs(symbolic - finite_diff) < ev.mp.mpf("1e-8") * max(
        abs(symbolic), 1
    )


def test_unary_plus_derivative(ev) -> None:
    dast = differentiate(parse_expression("+x"))
    assert ev.point_mp(dast, ev.mp.mpf(3)) == 1


def test_constant_derivative_is_zero(ev) -> None:
    dast = differentiate(parse_expression("pi*x + e"))
    # pi*x + e  -> pi; evaluated at a point should equal pi.
    val = ev.point_mp(dast, ev.mp.mpf(0))
    assert abs(val - ev.mp.pi) < ev.mp.mpf("1e-40")


def test_exponent_decrement_helper() -> None:
    from app.core.derivative import _decrement_literal

    assert _decrement_literal("3") == "2"
    assert _decrement_literal("0") == "-1"
    assert float(_decrement_literal("2.5")) == pytest.approx(1.5)


# ---------------------------------------------------------------------------
# Point evaluation (plain mpf path)
# ---------------------------------------------------------------------------
def test_point_eval_all_functions(ev) -> None:
    cases = {
        "sin(x)": ev.mp.sin,
        "cos(x)": ev.mp.cos,
        "tan(x)": ev.mp.tan,
        "exp(x)": ev.mp.exp,
        "log(x)": ev.mp.log,
        "sqrt(x)": ev.mp.sqrt,
    }
    for expr, fn in cases.items():
        p = ev.mp.mpf("0.7")
        assert abs(ev.point_mp(parse_expression(expr), p) - fn(p)) < ev.mp.mpf(
            "1e-40"
        )


def test_point_eval_constants_and_unary(ev) -> None:
    assert ev.point_mp(parse_expression("pi"), ev.mp.mpf(0)) == ev.mp.pi
    assert ev.point_mp(parse_expression("-5"), ev.mp.mpf(0)) == -5
    assert ev.point_mp(parse_expression("+2"), ev.mp.mpf(0)) == 2


def test_point_eval_division_and_integer_power(ev) -> None:
    val = ev.point_mp(parse_expression("x^3/2"), ev.mp.mpf(2))
    assert val == 4


def test_point_eval_noninteger_power_negative_is_domain_error(ev) -> None:
    from app.core.errors import DomainEvaluationError

    with pytest.raises(DomainEvaluationError):
        ev.point_mp(parse_expression("x^0.5"), ev.mp.mpf(-1))


def test_point_log_nonpositive_is_domain_error(ev) -> None:
    from app.core.errors import DomainEvaluationError

    with pytest.raises(DomainEvaluationError):
        ev.point_mp(parse_expression("log(x)"), ev.mp.mpf(0))


def test_point_iv_returns_thin_enclosure(ev) -> None:
    result = ev.point_iv(parse_expression("x^2"), ev.mp.mpf(3))
    lo_t, hi_t = result._mpi_
    assert lo_t == hi_t  # thin
    assert abs(ev.mp.mpf(lo_t) - 9) < ev.mp.mpf("1e-40")


# ---------------------------------------------------------------------------
# Interval ops
# ---------------------------------------------------------------------------
def test_contains_zero_and_signs(io) -> None:
    assert io.contains_zero(_mpi(io, -1, 1))
    assert not io.contains_zero(_mpi(io, 1, 2))
    assert io.strictly_positive(_mpi(io, 0.5, 1))
    assert io.strictly_negative(_mpi(io, -2, -0.1))
    assert not io.strictly_positive(_mpi(io, 0, 1))


def test_intersect_disjoint_and_overlap(io) -> None:
    assert io.intersect(_mpi(io, 0, 1), _mpi(io, 2, 3)) is None
    overlap = io.intersect(_mpi(io, 0, 2), _mpi(io, 1, 3))
    lo, hi = io.endpoints(overlap)
    assert lo == 1 and hi == 2


def test_strict_inside(io) -> None:
    outer = _mpi(io, 0, 10)
    assert io.strictly_inside(_mpi(io, 1, 9), outer)
    assert not io.strictly_inside(_mpi(io, 0, 9), outer)
    assert not io.strictly_inside(_mpi(io, 1, 10), outer)


def test_split_shares_midpoint(io) -> None:
    left, right = io.split(_mpi(io, 0, 10))
    _, lhi = io.endpoints(left)
    rlo, _ = io.endpoints(right)
    assert lhi == rlo  # closed halves share the midpoint


def test_exact_zero_predicate(io, ev) -> None:
    assert io.is_exact_zero(_mpi(io, 0, 0))
    assert not io.is_exact_zero(_mpi(io, "-1e-30", "1e-30"))


def test_is_infinite_on_tan_pole(io) -> None:
    ast = parse_expression("tan(x)")
    r = ev_interval(io, ast, "1.5", "1.7")
    assert io.is_infinite(r)


def ev_interval(io, ast, a, b):
    from app.core.evaluator import Evaluator  # noqa: F401

    return io.ev.interval(ast, _mpi(io, a, b))


def test_outward_decimal_containment_random(io) -> None:
    import random
    from decimal import Decimal

    random.seed(42)
    ev_mp = io.ev.mp
    for _ in range(500):
        a = ev_mp.mpf(random.uniform(-10, 10))
        b = a + ev_mp.mpf(10) ** ev_mp.mpf(random.uniform(-30, 0))
        lo_s, hi_s = io.outward_decimal(io.make(a, b), 30)
        assert ev_mp.mpf(lo_s) <= a
        assert ev_mp.mpf(hi_s) >= b
        assert Decimal(lo_s) <= Decimal(hi_s)


def test_midpoint_and_width(io) -> None:
    interval = _mpi(io, 2, 4)
    lo, hi = io.endpoints(io.midpoint(interval))
    assert abs(lo - 3) < io.ev.mp.mpf("1e-45")
    assert io.width(interval) == 2
