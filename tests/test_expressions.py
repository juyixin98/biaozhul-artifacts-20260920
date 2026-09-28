"""Expression layer: parsing restrictions, evaluation, differentiation,
and domain errors with preserved locations."""

import pytest
from mpmath import mp, mpf

from interval_cert.errors import (
    DomainEvaluationError,
    ExpressionSyntaxError,
    UnsupportedExpressionError,
)
from interval_cert.expressions import (
    differentiate,
    evaluate_interval,
    evaluate_point,
    expression_to_source,
    parse_expression,
)
from interval_cert.intervals import make_interval


def test_parse_rejects_empty_and_bad_syntax():
    with pytest.raises(ExpressionSyntaxError):
        parse_expression("", "x")
    with pytest.raises(ExpressionSyntaxError):
        parse_expression("x +", "x")


def test_parse_rejects_unsupported_constructs():
    with pytest.raises(UnsupportedExpressionError):
        parse_expression("tan(x)", "x")  # tan is not in the fragment
    with pytest.raises(UnsupportedExpressionError):
        parse_expression("y + 1", "x")  # unknown variable
    with pytest.raises(UnsupportedExpressionError):
        parse_expression("abs(x)", "x")  # not continuously differentiable
    with pytest.raises(UnsupportedExpressionError):
        parse_expression("x % 2", "x")


def test_caret_is_power_alias():
    node = parse_expression("x^2 - 2", "x")
    assert evaluate_point(node, "x", 2) == 2


def test_point_evaluation_matches_math():
    node = parse_expression("sin(x) + exp(-x) * log(x)", "x")
    with mp.workdps(50):
        x = mpf("1.7")
        expected = mp.sin(x) + mp.exp(-x) * mp.log(x)
        assert abs(evaluate_point(node, "x", x) - expected) < mpf("1e-45")


def test_interval_evaluation_encloses_range():
    node = parse_expression("x^2 - 2", "x")
    box = make_interval(0, 2)
    fx = evaluate_interval(node, "x", box)
    assert fx.a <= -2 and fx.b >= 2


def test_symbolic_derivative_matches_mpmath_numerical_derivative():
    # Independent oracle: mpmath.diff (not our differentiation code).
    node = parse_expression("x^3 * exp(x) - sin(x) / x", "x")
    dnode = differentiate(node, "x")
    with mp.workdps(50):
        x = mpf("1.3")
        f = lambda t: t**3 * mp.exp(t) - mp.sin(t) / t
        expected = mp.diff(f, x)
        got = evaluate_point(dnode, "x", x)
        assert abs(got - expected) < mpf("1e-40")


def test_domain_error_preserves_location():
    # log sits under root/right inside "x + log(x - 2)".
    node = parse_expression("x + log(x - 2)", "x")
    with pytest.raises(DomainEvaluationError) as excinfo:
        evaluate_interval(node, "x", make_interval(0, 1))
    err = excinfo.value
    assert err.category == "domain_error"
    assert err.details["location"] == "root/right"
    assert "interval" in err.details
    assert err.details["reason"] == "argument_not_positive"


def test_division_by_zero_straddling_interval_has_location():
    node = parse_expression("1 + 1 / x", "x")
    with pytest.raises(DomainEvaluationError) as excinfo:
        evaluate_interval(node, "x", make_interval(-1, 2))
    assert excinfo.value.details["location"] == "root/right"
    assert excinfo.value.details["reason"] == "divisor_straddles_zero"


def test_sqrt_of_negative_interval_is_domain_error():
    node = parse_expression("sqrt(x)", "x")
    with pytest.raises(DomainEvaluationError):
        evaluate_interval(node, "x", make_interval(-2, -1))


def test_noninteger_power_requires_positive_base():
    node = parse_expression("x ** 0.5", "x")
    with pytest.raises(DomainEvaluationError):
        evaluate_interval(node, "x", make_interval(-1, 4))
    ok = evaluate_interval(node, "x", make_interval(1, 4))
    assert ok.a <= 1 and ok.b >= 2


def test_derivative_stays_inside_language():
    node = parse_expression("sqrt(x) + log(x) + x^5", "x")
    dnode = differentiate(node, "x")
    # The derivative AST must itself parse as a supported expression.
    parse_expression(expression_to_source(dnode), "x")
