"""Lexer unit tests: quote/escape/comment masking with exact offsets."""
from __future__ import annotations

import pytest

from app.corpus import default_lexicon
from app.mining.lexer import CLOSE, OPEN, Lexer, OpenMask


@pytest.fixture
def lexer():
    return Lexer(default_lexicon())


def test_plain_brackets_tokenize(lexer):
    tokens, spans = lexer.scan("([{}])")
    assert [(t.offset, t.kind, t.type) for t in tokens] == [
        (0, OPEN, "paren"),
        (1, OPEN, "square"),
        (2, OPEN, "brace"),
        (3, CLOSE, "brace"),
        (4, CLOSE, "square"),
        (5, CLOSE, "paren"),
    ]
    assert spans == []


def test_brackets_in_quotes_are_not_tokens(lexer):
    tokens, spans = lexer.scan('x = "([)]" ;')
    assert tokens == []
    assert len(spans) == 1
    assert spans[0].start == 4 and spans[0].end == 10 and spans[0].terminated


def test_escaped_delimiter_does_not_close_span(lexer):
    # "a\"b([)]"  -- the only real closing quote is at the very end
    text = '"a\\"b([)]"'
    tokens, spans = lexer.scan(text)
    assert tokens == []
    assert spans[0].terminated
    assert spans[0].end == len(text)


def test_escaped_backslash_then_quote_closes(lexer):
    text = '"\\\\"([)]'
    tokens, spans = lexer.scan(text)
    assert len(spans) == 1 and spans[0].end == 4
    assert [t.offset for t in tokens] == [4, 5, 6, 7]


def test_unterminated_quote_masks_rest(lexer):
    tokens, spans = lexer.scan('"abc(')
    assert tokens == []
    assert not spans[0].terminated


def test_line_comment_masks_to_newline_only(lexer):
    tokens, spans = lexer.scan("// ([)]\n()")
    assert [t.offset for t in tokens] == [8, 9]
    assert spans[0].kind == "comment" and spans[0].end == 8


def test_block_comment_crosses_newlines(lexer):
    tokens, spans = lexer.scan("/* a\nb ( [ ) ] */x")
    assert tokens == []
    assert spans[0].terminated


def test_scan_block_carries_open_quote_across_boundary(lexer):
    part_a = 'abc("x[ '   # quote opens at 4, never closes in this block
    toks_a, spans_a, tail_a = lexer.scan_block(part_a, None)
    assert tail_a == OpenMask("quote", default_lexicon().quotes[0])
    assert [t.offset for t in toks_a] == [3]  # '(' structural, '[' masked

    part_b = ') ]y" ( )'
    toks_b, spans_b, tail_b = lexer.scan_block(part_b, tail_a)
    # Continuing quote masks ') ]y' (offsets 0..3); quote closes at 4; then
    # '(' at 6 and ')' at 8 are structural and match.
    assert tail_b is None
    assert [t.offset for t in toks_b] == [6, 8]


def test_scan_block_carries_open_block_comment(lexer):
    toks_a, _, tail_a = lexer.scan_block("a /* [ (", None)
    assert tail_a is not None and tail_a.kind == "block"
    toks_b, spans, tail_b = lexer.scan_block(") ] */ ()", tail_a)
    # "*/" occupies offsets 4..5, content before it masked; '(' 7, ')' 8.
    assert tail_b is None
    assert [t.offset for t in toks_b] == [7, 8]


def test_line_comment_does_not_cross_boundary(lexer):
    # A line comment open at a block edge with no newline still terminates
    # the block, but must NOT mask the next block.
    toks_a, _, tail_a = lexer.scan_block("// ( [", None)
    assert toks_a == [] and tail_a is None
    toks_b, _, tail_b = lexer.scan_block("()", None)
    assert len(toks_b) == 2 and tail_b is None
