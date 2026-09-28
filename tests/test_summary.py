"""Summary/composition tests.

Key boundary under test: composition preserves type ORDER. Net per-type
counts would call `([)]` and `)(` balanced; the ordered residual sequences
must not.
"""

from __future__ import annotations

from bracket_index.kernel.lexer import CLOSE, OPEN, Token, lex
from bracket_index.kernel.summary import compose, compose_all, summarize


def tok(kind, btype, pos):
    return Token(kind=kind, btype=btype, pos=pos)


def test_summarize_balanced_chunk_is_empty():
    assert summarize(lex("(a)[b]")).balanced


def test_net_counts_equal_but_cross_mismatch_is_not_balanced():
    # "([)]": one open and one close per type — net counts all zero.
    summary = summarize(lex("([)]"))
    assert not summary.balanced
    assert len(summary.mismatches) == 1
    mismatch = summary.mismatches[0]
    assert (mismatch.open.btype, mismatch.open.pos) == ("[", 1)
    assert (mismatch.close.btype, mismatch.close.pos) == ("(", 2)


def test_reversed_pair_is_not_balanced_despite_zero_net_count():
    summary = summarize(lex(")("))
    assert not summary.balanced
    assert [t.pos for t in summary.closers] == [0]
    assert [t.pos for t in summary.openers] == [1]


def test_compose_cancels_matching_types_across_chunks():
    left = summarize(lex("a("))      # trailing opener (
    right = summarize(lex(")b"))     # leading closer )
    combined = compose(left, right)
    assert combined.balanced


def test_compose_preserves_type_order_mismatch_across_chunks():
    # "(" in chunk 1, "]" in chunk 2: zero net counts, wrong type order.
    left = summarize(lex("(["))
    right = summarize(lex(")]"))
    combined = compose(left, right)
    assert not combined.balanced
    assert len(combined.mismatches) == 1
    assert combined.mismatches[0].open.btype == "["
    assert combined.mismatches[0].close.btype == "("


def test_compose_is_associative_on_nested_blocks():
    text = "fn(a, [b, {c: (d)}], e){f[0] = (g)}"
    tokens = lex(text)
    whole = summarize(tokens)
    # Split the token stream three different ways; all must agree.
    for cut1, cut2 in ((1, 5), (3, 9), (7, 12)):
        parts = compose_all(
            [
                summarize(tokens[:cut1]),
                summarize(tokens[cut1:cut2]),
                summarize(tokens[cut2:]),
            ]
        )
        assert parts == whole


def test_compose_all_matches_whole_scan_on_mismatched_text():
    tokens = lex("x([y)]z{")
    whole = summarize(tokens)
    for cut in range(len(tokens) + 1):
        parts = compose(summarize(tokens[:cut]), summarize(tokens[cut:]))
        assert parts == whole


def test_mismatch_policy_drops_closer_and_recovers():
    # After the mismatched ")" is dropped, "]" still matches "[".
    summary = summarize(lex("([)]"))
    assert [t.pos for t in summary.openers] == [0]  # "(" left over
    assert [t.pos for t in summary.closers] == []
