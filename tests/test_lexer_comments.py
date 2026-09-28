"""注释与换行：行注释跳过、固定 LF 换行模式、`.` 不含换行。"""

from app.kernel.lexer import CompiledLexer, Rule

COMMENT_SPEC = [
    ("LINE_COMMENT", "//[^\\n]*", 0, True),
    ("HASH_COMMENT", "#[^\\n]*", 0, True),
    ("WS", "[ \\t\\n]+", 0, True),
    ("IDENT", "[A-Za-z_][A-Za-z0-9_]*", 0, False),
    ("INT", "[0-9]+", 0, False),
]


def build(limits, logger):
    rules = [Rule(i, n, p, prio, skip) for i, (n, p, prio, skip) in enumerate(COMMENT_SPEC)]
    return CompiledLexer.build(rules, limits, logger)


def test_comments_are_skipped_offsets_preserved(limits, logger):
    lexer = build(limits, logger)
    text = "a // hi\nb # c\nc"
    tokens = lexer.tokenize(text, limits, logger)
    got = [(t.type, t.start, t.end, t.text) for t in tokens]
    # 手工参考：注释与空白被跳过，词元偏移仍指向原文
    assert got == [
        ("IDENT", 0, 1, "a"),
        ("IDENT", 8, 9, "b"),
        ("IDENT", 14, 15, "c"),
    ]
    for t in tokens:
        assert text[t.start : t.end] == t.text


def test_comment_runs_to_newline_not_past_it(limits, logger):
    lexer = build(limits, logger)
    # 注释止于 \n（固定 LF 换行模式），下一行内容正常成词
    tokens = lexer.tokenize("// only comment\nx", limits, logger)
    assert [(t.type, t.start, t.end, t.text) for t in tokens] == [("IDENT", 16, 17, "x")]


def test_carriage_return_is_not_newline(limits, logger):
    lexer = build(limits, logger)
    # \r 不是换行：// 注释会吞掉 \r 直到 \n
    tokens = lexer.tokenize("// c\r\ny", limits, logger)
    assert [(t.type, t.text) for t in tokens] == [("IDENT", "y")]
