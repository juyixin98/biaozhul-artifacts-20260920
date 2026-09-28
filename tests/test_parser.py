"""Parser grammar and error-position tests."""

from __future__ import annotations

import pytest

from app.core.ast import Binary, Call, Num, Paren, Unary, Var
from app.core.errors import ErrorCode
from app.core.parser import parse_expression


def test_parses_simple_polynomial() -> None:
    node = parse_expression("x^2 - 3*x + 1")
    assert isinstance(node, Binary)
    assert node.op == "+"
    assert node.start == 0
    assert node.end == len("x^2 - 3*x + 1")


def test_power_binds_tighter_than_unary_minus() -> None:
    # -x^2 must parse as -(x^2), not (-x)^2.
    node = parse_expression("-x^2")
    assert isinstance(node, Unary)
    assert node.op == "-"
    assert isinstance(node.operand, Binary)
    assert node.operand.op == "^"


def test_parentheses_preserved_for_spans() -> None:
    node = parse_expression("(x+1)")
    assert isinstance(node, Paren)
    assert node.start == 0
    assert node.end == 5
    assert isinstance(node.inner, Binary)


def test_function_call_span() -> None:
    node = parse_expression("sin(x)")
    assert isinstance(node, Call)
    assert node.name == "sin"
    assert node.start == 0 and node.end == 6  # "sin(x)" spans 6 chars


@pytest.mark.parametrize(
    "text,snippet",
    [
        ("2x", "2x"),          # implicit multiplication
        ("x ^^ 2", "^"),       # stray operator
        ("sin(x", "x"),        # unbalanced paren: points at last token/EOF
        ("foo(x)", "foo"),     # unknown identifier
        ("x @ 2", "@"),        # illegal character
    ],
)
def test_parse_errors_carry_code_and_position(text: str, snippet: str) -> None:
    from app.core.errors import ParseError

    with pytest.raises(ParseError) as exc_info:
        parse_expression(text)
    err = exc_info.value
    assert err.code is ErrorCode.PARSE_ERROR
    assert err.category.value == "input"
    assert err.position is not None
    assert err.position.start < err.position.end
    if snippet:
        assert snippet in err.position.snippet or snippet == err.position.snippet


def test_exponent_must_be_numeric_literal() -> None:
    from app.core.errors import ParseError

    with pytest.raises(ParseError) as exc_info:
        parse_expression("x^x")
    assert exc_info.value.code is ErrorCode.PARSE_ERROR


def test_negative_integer_exponent_parses() -> None:
    node = parse_expression("x^-3")
    assert isinstance(node, Binary)
    assert isinstance(node.right, Unary)
    assert isinstance(node.right.operand, Num)
    assert node.right.operand.text == "3"


def test_unknown_variable_rejected() -> None:
    from app.core.errors import ParseError

    with pytest.raises(ParseError) as exc_info:
        parse_expression("y + 1")
    assert "y" == exc_info.value.position.snippet


def test_number_then_letter_rejected_with_position() -> None:
    from app.core.errors import ParseError

    with pytest.raises(ParseError) as exc_info:
        parse_expression("3sin(x)")
    pos = exc_info.value.position
    assert pos.start == 0
