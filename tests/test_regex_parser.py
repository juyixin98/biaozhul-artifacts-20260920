"""正则解析：合法结构的 AST 形状、语法错误与资源错误类别。"""

import pytest

from app.errors import ErrorCategory, InputError, ResourceExhausted
from app.kernel.regex_ast import Alt, Char, Concat, Repeat, parse_pattern


def test_parse_concat_and_alt(limits):
    ast = parse_pattern("ab|cd", limits)
    assert isinstance(ast, Alt)
    assert len(ast.options) == 2
    assert all(isinstance(o, Concat) for o in ast.options)


def test_parse_repeat_bounds(limits):
    ast = parse_pattern("a{2,4}", limits)
    assert isinstance(ast, Repeat)
    assert (ast.min, ast.max) == (2, 4)


def test_parse_escape_newline(limits):
    ast = parse_pattern("\\n", limits)
    assert isinstance(ast, Char)
    assert ast.cs.intervals == ((0x0A, 0x0A),)


def test_dot_excludes_newline(limits):
    ast = parse_pattern(".", limits)
    assert isinstance(ast, Char)
    assert not ast.cs.contains(0x0A)
    assert ast.cs.contains(ord("a"))


def test_class_negation(limits):
    ast = parse_pattern("[^\\n]", limits)
    assert isinstance(ast, Char)
    assert not ast.cs.contains(0x0A)
    assert ast.cs.contains(ord("x"))


@pytest.mark.parametrize(
    "pattern",
    ["a{2,1}", "(a", "a)", "[a", "a\\", "*a", "a{,}", "\\q"],
)
def test_syntax_errors_are_input_errors(limits, pattern):
    with pytest.raises(InputError) as exc:
        parse_pattern(pattern, limits)
    assert exc.value.category is ErrorCategory.INPUT_ERROR
    assert exc.value.code == "REGEX_SYNTAX"
    assert "position" in exc.value.details


def test_repeat_over_limit_is_resource_exhausted(limits):
    with pytest.raises(ResourceExhausted) as exc:
        parse_pattern("a{2000}", limits)
    assert exc.value.category is ErrorCategory.RESOURCE_EXHAUSTED
    assert exc.value.code == "REPEAT_TOO_LARGE"


def test_empty_pattern_is_input_error(limits):
    with pytest.raises(InputError) as exc:
        parse_pattern("", limits)
    assert exc.value.code == "EMPTY_PATTERN"
