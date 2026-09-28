"""数字格式：整数/浮点/十六进制的最长匹配；词法失败的偏移与错误类别。"""

import pytest

from app.errors import ErrorCategory, InputError
from app.kernel.lexer import CompiledLexer, Rule

NUMBER_SPEC = [
    ("INT", "[0-9]+", 0),
    ("FLOAT", "[0-9]+\\.[0-9]+", 0),
    ("HEX", "0x[0-9a-fA-F]+", 0),
    ("WS", "[ \\t\\n]+", 0),
]


def build(limits, logger):
    rules = [
        Rule(i, n, p, prio, skip=(n == "WS"))
        for i, (n, p, prio) in enumerate(NUMBER_SPEC)
    ]
    return CompiledLexer.build(rules, limits, logger)


def test_number_formats(limits, logger):
    lexer = build(limits, logger)
    text = "42 3.14 0xFF 007"
    tokens = lexer.tokenize(text, limits, logger)
    got = [(t.type, t.start, t.end, t.text) for t in tokens]
    assert got == [
        ("INT", 0, 2, "42"),
        ("FLOAT", 3, 7, "3.14"),   # 最长匹配：整体浮点，而非 INT(3) 后报错
        ("HEX", 8, 12, "0xFF"),
        ("INT", 13, 16, "007"),
    ]


def test_float_beats_int_by_length_not_priority(limits, logger):
    lexer = build(limits, logger)
    # INT 与 FLOAT 同优先级 0：FLOAT 凭更长匹配胜出
    tokens = lexer.tokenize("1.5", limits, logger)
    assert [(t.type, t.text) for t in tokens] == [("FLOAT", "1.5")]


def test_trailing_dot_is_lex_error_with_offset(limits, logger):
    lexer = build(limits, logger)
    with pytest.raises(InputError) as exc:
        lexer.tokenize("12.", limits, logger)
    assert exc.value.category is ErrorCategory.INPUT_ERROR
    assert exc.value.code == "LEX_NO_MATCH"
    assert exc.value.details["offset"] == 2
    assert exc.value.details["char"] == "."


def test_unmatchable_first_char_reports_offset_zero(limits, logger):
    lexer = build(limits, logger)
    with pytest.raises(InputError) as exc:
        lexer.tokenize("@1", limits, logger)
    assert exc.value.details["offset"] == 0
