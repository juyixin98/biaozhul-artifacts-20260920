"""关键字与标识符：最长匹配优先于优先级；等长时优先级决胜。

期望词元为手工书写的参考答案；另用 Python re 独立复核词元文本归属。
"""

import re

from app.kernel.lexer import Rule

from conftest import build_lexer

KEYWORD_SPEC = [
    ("IF", "if", 10),
    ("ELSE", "else", 10),
    ("WHILE", "while", 10),
    ("IDENT", "[A-Za-z_][A-Za-z0-9_]*", 0),
    ("WS", "[ \\t\\n]+", 0),
]


def build(limits, logger):
    rules = [
        Rule(i, n, p, prio, skip=(n == "WS"))
        for i, (n, p, prio) in enumerate(KEYWORD_SPEC)
    ]
    from app.kernel.lexer import CompiledLexer

    return CompiledLexer.build(rules, limits, logger)


def test_keyword_vs_identifier_offsets(limits, logger):
    lexer = build(limits, logger)
    text = "if iffy else\nwhile x1"
    tokens = lexer.tokenize(text, limits, logger)
    got = [(t.type, t.start, t.end, t.text) for t in tokens]
    # 手工参考答案（偏移按码点计）
    assert got == [
        ("IF", 0, 2, "if"),
        ("IDENT", 3, 7, "iffy"),   # 最长匹配：iffy 整体是标识符，而非 if+错误
        ("ELSE", 8, 12, "else"),
        ("WHILE", 13, 18, "while"),
        ("IDENT", 19, 21, "x1"),
    ]
    # 偏移与原文一致（原偏移契约）
    for t in tokens:
        assert text[t.start : t.end] == t.text


def test_same_length_keyword_wins_by_priority(limits, logger):
    lexer = build(limits, logger)
    # "if" 同时被 IF(prio 10) 与 IDENT(prio 0) 等长匹配 -> 优先级决胜
    tokens = lexer.tokenize("if", limits, logger)
    assert [(t.type, t.text) for t in tokens] == [("IF", "if")]


def test_token_text_matches_independent_regex_oracle(limits, logger):
    """用 Python re 作为独立预言机复核每个词元的归属类别。"""
    lexer = build(limits, logger)
    text = "while foo9 else _bar if"
    tokens = lexer.tokenize(text, limits, logger)
    oracle = {
        "IF": re.compile(r"if"),
        "ELSE": re.compile(r"else"),
        "WHILE": re.compile(r"while"),
        "IDENT": re.compile(r"[A-Za-z_][A-Za-z0-9_]*"),
    }
    assert [t.type for t in tokens] == ["WHILE", "IDENT", "ELSE", "IDENT", "IF"]
    for t in tokens:
        assert re.fullmatch(oracle[t.type], t.text)
