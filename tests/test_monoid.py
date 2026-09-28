"""Monoid tests for chunk summaries: order is retained, counts are not enough."""
from __future__ import annotations

import itertools
import random

from app.mining.lexer import CLOSE, OPEN, Token
from app.mining.tokens import (
    EMPTY,
    combine,
    compose,
    net_counts,
    reduce_all,
    reduce_tokens,
)


def _tok(offset, kind, type_name):
    ch = {
        "open": {"paren": "(", "square": "[", "brace": "{"},
        "close": {"paren": ")", "square": "]", "brace": "}"},
    }[kind][type_name]
    return Token(offset, kind, type_name, ch)


def test_crossing_types_equal_counts_but_not_balanced():
    # ( [ ) ] : one of each type, net counts all zero, yet two mismatches.
    tokens = [
        _tok(0, OPEN, "paren"), _tok(1, OPEN, "square"),
        _tok(2, CLOSE, "paren"), _tok(3, CLOSE, "square"),
    ]
    r = reduce_tokens(tokens)
    counts = net_counts(r)
    assert counts.get("paren", 0) == 0 and counts.get("square", 0) == 0
    assert not r.balanced
    assert len(r.mismatches) == 2


def test_compose_matches_full_reduction_for_known_streams():
    streams = [
        "()[]",
        "(())",
        "([)]",
        "(()",
        "())",
        ")(",
        "([]){}",
        "([)]{}(",
    ]
    type_of = {"(": ("open", "paren"), ")": ("close", "paren"),
               "[": ("open", "square"), "]": ("close", "square"),
               "{": ("open", "brace"), "}": ("close", "brace")}
    for s in streams:
        tokens = [_tok(i, *type_of[ch]) for i, ch in enumerate(s)]
        whole = reduce_tokens(tokens)
        for cut in range(len(tokens) + 1):
            left = reduce_tokens(tokens[:cut])
            # Right side carries block-LOCAL offsets (origin at cut).
            right_tokens = [
                Token(t.offset - cut, t.kind, t.type, t.char)
                for t in tokens[cut:]
            ]
            right = reduce_tokens(right_tokens)
            glued = combine(left, right, cut)
            assert _event_offsets(glued) == _event_offsets(whole), (s, cut)
            assert _ends(glued) == _ends(whole), (s, cut)


def test_associativity_on_random_token_streams():
    rng = random.Random(42)
    types = ["paren", "square", "brace"]
    for _ in range(200):
        n = rng.randint(3, 18)
        tokens = []
        for i in range(n):
            kind = "open" if rng.random() < 0.5 else "close"
            tokens.append(_tok(i, kind, rng.choice(types)))
        size = rng.choice([1, 2, 3])
        chunks = _split_every(tokens, size)
        if len(chunks) < 3:
            continue
        # Per-chunk reductions with chunk-local offsets; combine carries the
        # shift, so both bracketings must produce identical global events.
        reductions = [
            reduce_tokens([Token(j, t.kind, t.type, t.char)
                           for j, t in enumerate(chunk)])
            for chunk in chunks
        ]
        sizes = [len(c) for c in chunks]
        left = combine(reductions[0], reductions[1], sizes[0])
        acc_left = left
        acc_offset = sizes[0] + sizes[1]
        a = combine(acc_left, reductions[2], acc_offset)
        right = combine(reductions[1], reductions[2], sizes[1])
        b = combine(reductions[0], right, sizes[0])
        assert _event_offsets(a) == _event_offsets(b)


def test_empty_reduction_is_identity():
    r = reduce_tokens([_tok(0, OPEN, "paren"), _tok(1, CLOSE, "paren")])
    assert _event_offsets(compose(EMPTY, r)) == _event_offsets(r)
    assert _event_offsets(compose(r, EMPTY)) == _event_offsets(r)
    assert reduce_all([]) == EMPTY


def _event_offsets(r):
    return (
        sorted((o.offset, c.offset) for o, c in r.matches),
        sorted((o.offset, c.offset) for o, c in r.mismatches),
        [t.offset for t in r.prefix],
        [t.offset for t in r.suffix],
    )


def _ends(r):
    return ([t.offset for t in r.prefix], [t.offset for t in r.suffix])


def _split_every(items, size):
    return [items[i:i + size] for i in range(0, len(items), size)] \
        or [[]]
