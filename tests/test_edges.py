"""Edge coverage: interval domain paths, parser branches, approximation."""

from __future__ import annotations

import pytest

from app.core.derivative import differentiate
from app.core.evaluator import Evaluator
from app.core.errors import DomainEvaluationError
from app.core.parser import parse_expression


@pytest.fixture
def ev():
    return Evaluator(50)


def _iv(ev, expr, a, b):
    ast = parse_expression(expr)
    x = ev.iv.mpf([ev.mp.mpf(a), ev.mp.mpf(b)])
    return ev.interval(ast, x)


def test_noninteger_power_negative_interval_is_domain_error(ev) -> None:
    with pytest.raises(DomainEvaluationError):
        _iv(ev, "x^0.5", "-2", "-1")


def test_zero_base_negative_power_interval(ev) -> None:
    # [0,1]^-1 -> extended division reaches +inf (finite at 1), no crash.
    result = _iv(ev, "x^-1", "0", "1")
    lo, hi = result._mpi_[0], result._mpi_[1]
    import mpmath

    assert mpmath.isinf(mpmath.mpf(hi))


def test_interval_sqrt_log_exp_trig(ev) -> None:
    import mpmath

    assert _iv(ev, "sqrt(x)", "1", "4").a <= mpmath.mpf(1)
    log_r = _iv(ev, "log(x)", "1", "2")
    assert log_r.a <= 0 <= log_r.b
    exp_r = _iv(ev, "exp(x)", "0", "1")
    assert 1 <= exp_r.a and exp_r.b >= mpmath.e
    sin_r = _iv(ev, "sin(x)", "0", "1")
    cos_r = _iv(ev, "cos(x)", "0", "1")
    assert sin_r.a >= 0
    assert cos_r.b <= 1


def test_interval_constants(ev) -> None:
    pi_r = _iv(ev, "pi", "0", "1")
    assert pi_r.a <= ev.mp.pi <= pi_r.b


def test_derivative_tan_chain_rule(ev) -> None:
    # d tan(2x) = 2(1+tan^2(2x)); check it evaluates at a finite point.
    dast = differentiate(parse_expression("tan(2*x)"))
    p = ev.mp.mpf("0.2")
    val = ev.point_mp(dast, p)
    expected = 2 * (1 + ev.mp.tan(2 * p) ** 2)
    assert abs(val - expected) < ev.mp.mpf("1e-30")


def test_derivative_log_and_sqrt_and_exp(ev) -> None:
    for expr, point in [("log(x)", "1.5"), ("sqrt(x)", "3.0"), ("exp(x)", "0.4")]:
        dast = differentiate(parse_expression(expr))
        p = ev.mp.mpf(point)
        h = ev.mp.mpf("1e-18")
        f = parse_expression(expr)
        fd = (ev.point_mp(f, p + h) - ev.point_mp(f, p - h)) / (2 * h)
        assert abs(ev.point_mp(dast, p) - fd) < ev.mp.mpf("1e-12")


# ---------------------------------------------------------------------------
# Parser branches
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text",
    [
        "x +",
        "* x",
        "/",
        "()",
        "x^",
        "sin",
        "sin(",
        "x + (1",
        "2..5",
        "1e",
        "+",
    ],
)
def test_parser_rejects_malformed(text) -> None:
    from app.core.errors import ParseError

    with pytest.raises(ParseError):
        parse_expression(text)


def test_scientific_notation_number() -> None:
    node = parse_expression("1.5e-3*x")
    ast = node  # 1.5e-3 * x
    ev = Evaluator(40)
    val = ev.point_mp(ast, ev.mp.mpf(2))
    assert abs(val - ev.mp.mpf("0.003")) < ev.mp.mpf("1e-30")


def test_empty_expression_rejected() -> None:
    from app.core.errors import ParseError

    with pytest.raises(ParseError):
        parse_expression("   ")


# ---------------------------------------------------------------------------
# Approximation layer edge paths
# ---------------------------------------------------------------------------
def test_approximation_finds_interior_root() -> None:
    from app.core.approximation import approximate_roots
    from app.core.config import CertConfig
    from app.core.tracer import Tracer

    ev = Evaluator(40)
    ast = parse_expression("x^3 - x")
    with Tracer.create(None) as tracer:
        report = approximate_roots(
            ast, ev, ev.mp.mpf(-2), ev.mp.mpf(2), CertConfig(),
            sample_count=2001,
        )
    values = sorted(r.value for r in report.roots)
    assert len(values) == 3
    assert abs(values[0] - (-1)) < 1e-9
    assert abs(values[1]) < 1e-9
    assert abs(values[2] - 1) < 1e-9


def test_approximation_root_exactly_on_grid_point() -> None:
    from app.core.approximation import approximate_roots
    from app.core.config import CertConfig
    from app.core.tracer import Tracer

    ev = Evaluator(40)
    ast = parse_expression("x")
    with Tracer.create(None):
        report = approximate_roots(
            ast, ev, ev.mp.mpf(0), ev.mp.mpf(10), CertConfig(),
            sample_count=11,  # grid includes 0
        )
    assert any(abs(r.value) < 1e-12 for r in report.roots)
