"""表达式解析器单元测试。"""

import math

import numpy as np
import pytest

from adaptive_integration.parser import compile_expression, ParseError


def eval_scalar(expr, x):
    return float(compile_expression(expr)(x))


def eval_array(expr, x):
    return np.asarray(compile_expression(expr)(np.asarray(x, dtype=float)))


# ---------- 基本运算 ----------

@pytest.mark.parametrize(
    "expr,x,expected",
    [
        ("1 + 2", 0, 3),
        ("2 - 3", 0, -1),
        ("2*3", 0, 6),
        ("7/2", 0, 3.5),
        ("2^10", 0, 1024),
        ("x + 1", 4, 5),
        ("-x", 3, -3),
        ("-2^2", 0, -4),          # 幂运算优先于一元负号
        ("(-2)^2", 0, 4),
        ("2^-2", 0, 0.25),
        ("2^2^3", 0, 256),        # 右结合
        ("-(1+2)^2", 0, -9),
        ("3 - 1 - 1", 0, 1),      # 左结合
        ("8/4/2", 0, 1),
        ("2*3+4", 0, 10),
        ("2+3*4", 0, 14),
        ("(2+3)*4", 0, 20),
        ("1.5e-2", 0, 0.015),
        ("2E+3", 0, 2000.0),
        (".5", 0, 0.5),
        ("pi", 0, math.pi),
        ("2*pi", 0, 2 * math.pi),
        ("e", 0, math.e),
    ],
)
def test_arithmetic(expr, x, expected):
    assert eval_scalar(expr, x) == pytest.approx(expected)


@pytest.mark.parametrize(
    "expr,x,expected",
    [
        ("sin(0)", 0, 0.0),
        ("cos(pi)", 0, -1.0),
        ("sqrt(16)", 0, 4.0),
        ("log(e^2)", 0, 2.0),
        ("abs(-5)", 0, 5.0),
        ("atan2(1, 1)", 0, math.pi / 4),
        ("exp(log(2))", 0, 2.0),
        ("sin(x)^2 + cos(x)^2", 1.3, 1.0),
        ("sqrt(abs(x))", -4, 2.0),
        ("log10(1000)", 0, 3.0),
    ],
)
def test_functions(expr, x, expected):
    assert eval_scalar(expr, x) == pytest.approx(expected, rel=1e-12)


def test_array_evaluation():
    x = np.linspace(-1, 1, 11)
    y = eval_array("x^2 - sin(x)", x)
    assert np.allclose(y, x**2 - np.sin(x))


def test_nonfinite_propagates():
    # 1/0 -> inf（不抛 Python 异常），由驱动层统一检测
    y = eval_scalar("1/x", 0.0)
    assert math.isinf(y)
    y2 = eval_scalar("sqrt(-1)", 0.0)
    assert math.isnan(y2)


# ---------- 错误表达式 ----------

@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "1 +",
        "* 2",
        "(1 + 2",
        "1 + 2)",
        "sin(",
        "sin(1, 2)",
        "atan2(1)",
        "foo(x)",
        "2x",
        "2(x+1)",
        "1..2",
        "x @ 2",
        "x +; 1",
    ],
)
def test_parse_errors(bad):
    with pytest.raises(ParseError):
        compile_expression(bad)


def test_expression_length_limit():
    with pytest.raises(ParseError):
        compile_expression("x" * 501)


def test_non_string():
    with pytest.raises(ParseError):
        compile_expression(123)  # type: ignore[arg-type]
