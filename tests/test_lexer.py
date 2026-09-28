"""Lexer tests: quotes and escape ranges must shield string content.

All expected token lists come from the hand-written corpus fixtures, not
from the implementation under test.
"""

from __future__ import annotations

import pytest

from bracket_index.corpus.fixtures import CASES
from bracket_index.kernel.lexer import lex


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
def test_lex_matches_handwritten_tokens(case):
    tokens = lex(case.text)
    got = [(t.kind, t.btype, t.pos) for t in tokens]
    expected = [(t.kind, t.btype, t.pos) for t in case.tokens]
    assert got == expected


def test_base_offset_shifts_positions():
    tokens = lex("()", base=100)
    assert [t.pos for t in tokens] == [100, 101]


def test_unterminated_string_produces_no_more_tokens():
    assert lex('( " ) ( [ ]') == lex('( " ignored')
