"""Chunk reductions and their composition monoid.

A stream of bracket tokens reduces to:

* ``prefix`` -- closing brackets that could not be matched while scanning the
  chunk even after earlier chunks' open brackets were supplied (in order);
* ``suffix`` -- opening brackets left open at the end (in order);
* ``match_events`` / ``mismatch_events`` -- pairs discovered while reducing.

Why keep the *typed sequences* rather than one net counter per type:
``( [ ) ]`` has one opener and one closer of every relevant type -- counts are
equal -- yet the stream is invalid. Sequence composition reproduces exactly
what a full stack scan decides, including the mismatch events.

Mismatch convention (shared with :mod:`app.mining.scanner`, so the chunk
pipeline and the complete stack scan always agree):

* closer with empty stack           -> stray closer, goes to ``prefix``;
* closer with same-typed top        -> match, opener popped;
* closer with differently typed top -> mismatch event; BOTH are discarded
  (the closer is consumed and the opener is popped). This is the convention
  used by editor "bracket jump" logic: the pair does not match and neither
  token remains available for a later partner.

``Reduction`` forms a monoid: ``compose`` is associative and the empty
reduction is its identity (asserted by property tests in the test suite).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .lexer import CLOSE, OPEN, Token

Pair = tuple[Token, Token]


@dataclass(frozen=True)
class Reduction:
    prefix: tuple[Token, ...] = ()
    suffix: tuple[Token, ...] = ()
    matches: Pair = field(default_factory=tuple)  # type: ignore[assignment]
    mismatches: Pair = field(default_factory=tuple)  # type: ignore[assignment]

    @property
    def balanced(self) -> bool:
        return not self.prefix and not self.suffix and not self.mismatches


EMPTY = Reduction()


def shift_token(tok: Token, delta: int) -> Token:
    return Token(tok.offset + delta, tok.kind, tok.type, tok.char)


def shift_reduction(r: Reduction, delta: int) -> Reduction:
    """Move every coordinate in ``r`` by ``delta``."""
    if delta == 0:
        return r
    return Reduction(
        prefix=tuple(shift_token(t, delta) for t in r.prefix),
        suffix=tuple(shift_token(t, delta) for t in r.suffix),
        matches=tuple(
            (shift_token(o, delta), shift_token(c, delta)) for o, c in r.matches
        ),
        mismatches=tuple(
            (shift_token(o, delta), shift_token(c, delta))
            for o, c in r.mismatches
        ),
    )


def reduce_tokens(tokens: list[Token] | tuple[Token, ...]) -> Reduction:
    """Reduce one token stream from an empty stack."""
    stack: list[Token] = []
    prefix: list[Token] = []
    matches: list[Pair] = []
    mismatches: list[Pair] = []
    for tok in tokens:
        if tok.kind == OPEN:
            stack.append(tok)
            continue
        if not stack:
            prefix.append(tok)
            continue
        top = stack[-1]
        if top.type == tok.type:
            stack.pop()
            matches.append((top, tok))
        else:
            stack.pop()
            mismatches.append((top, tok))
    return Reduction(tuple(prefix), tuple(stack), tuple(matches), tuple(mismatches))


def _merge_events(*event_lists: Pair) -> tuple[Pair, ...]:
    """Merge event lists ordered by the closer offset (stable)."""
    merged: list[Pair] = []
    for events in event_lists:
        merged.extend(events)
    merged.sort(key=lambda pair: pair[1].offset)
    return tuple(merged)


def compose(left: Reduction, right: Reduction) -> Reduction:
    """Combine two adjacent reductions with a zero-length seam.

    Equivalent to :func:`combine` when the caller guarantees the right-hand
    coordinates already follow the left-hand ones (used by the algebraic
    property tests, where coordinate origins are irrelevant).
    """
    return combine(left, right, 0)


def combine(left: Reduction, right: Reduction, left_len: int) -> Reduction:
    """Combine adjacent reductions where ``right`` starts at offset
    ``left_len`` relative to ``left``'s coordinate origin.

    Only ``left.suffix`` openers and ``right.prefix`` closers can interact
    across the boundary; re-running the stack machine over exactly that
    sequence (with right-side coordinates shifted by ``left_len``) yields the
    boundary events. Internal matches/mismatches stay valid (an internal
    opener always sits on top of anything from the left), and events are
    merged back in chronological order.
    """
    seam = list(left.suffix) + [shift_token(t, left_len) for t in right.prefix]
    boundary = reduce_tokens(seam)
    shifted_right = shift_reduction(right, left_len)
    return Reduction(
        prefix=left.prefix + boundary.prefix,
        suffix=boundary.suffix + shifted_right.suffix,
        matches=_merge_events(left.matches, boundary.matches, shifted_right.matches),
        mismatches=_merge_events(
            left.mismatches, boundary.mismatches, shifted_right.mismatches
        ),
    )


def reduce_all(reductions: list[Reduction]) -> Reduction:
    """Left fold of :func:`compose` over chunk summaries (empty = identity)."""
    acc = EMPTY
    for r in reductions:
        acc = compose(acc, r)
    return acc


def net_counts(r: Reduction) -> dict[str, int]:
    """Per-type net open counts.

    Exposed for diagnostics: equal counts are a NECESSARY condition only and
    must never be taken as proof of balance.
    """
    counts: dict[str, int] = {}
    for tok in r.suffix:
        counts[tok.type] = counts.get(tok.type, 0) + 1
    for tok in r.prefix:
        counts[tok.type] = counts.get(tok.type, 0) - 1
    return counts
