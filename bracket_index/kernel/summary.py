"""Chunk summaries and their composition.

A summary is the residual of scanning a token stream with a stack:

- `closers`: unmatched closing brackets, in order of appearance
- `openers`: unmatched opening brackets, in order of appearance
- `mismatches`: type-mismatch events, sorted by closing-bracket position

Mismatch policy (shared by the oracle and by composition): when a closing
bracket meets a non-empty stack whose top has a different type, the event is
recorded and the *closer* is dropped; the opener stays on the stack.

Composition preserves type order: the trailing openers of the left summary
cancel, from the right, against the leading closers of the right summary,
only while types match. A summary carrying only per-type net counts could not
distinguish `([)]` from `()[]`; the ordered residual sequences can.
"""

from __future__ import annotations

from dataclasses import dataclass

from .lexer import CLOSE, OPEN, Token


@dataclass(frozen=True)
class Mismatch:
    open: Token  # stack-top opener at the time of the event
    close: Token  # offending closing bracket (dropped)


@dataclass(frozen=True)
class Summary:
    closers: tuple[Token, ...]
    openers: tuple[Token, ...]
    mismatches: tuple[Mismatch, ...]

    @property
    def balanced(self) -> bool:
        return not self.closers and not self.openers and not self.mismatches


EMPTY_SUMMARY = Summary((), (), ())


def summarize(tokens: list[Token] | tuple[Token, ...]) -> Summary:
    stack: list[Token] = []
    closers: list[Token] = []
    mismatches: list[Mismatch] = []
    for tok in tokens:
        if tok.kind == OPEN:
            stack.append(tok)
        elif tok.kind == CLOSE:
            if not stack:
                closers.append(tok)
            elif stack[-1].btype == tok.btype:
                stack.pop()
            else:
                mismatches.append(Mismatch(open=stack[-1], close=tok))
        else:
            raise ValueError(f"unknown token kind: {tok.kind!r}")
    return Summary(tuple(closers), tuple(stack), tuple(mismatches))


def _merge_mismatches(
    left: list[Mismatch], right: tuple[Mismatch, ...]
) -> tuple[Mismatch, ...]:
    """Merge two mismatch lists, each sorted by closer position."""
    merged: list[Mismatch] = []
    i = j = 0
    while i < len(left) and j < len(right):
        if left[i].close.pos <= right[j].close.pos:
            merged.append(left[i])
            i += 1
        else:
            merged.append(right[j])
            j += 1
    merged.extend(left[i:])
    merged.extend(right[j:])
    return tuple(merged)


def compose(left: Summary, right: Summary) -> Summary:
    """Compose two adjacent summaries (left text precedes right text)."""
    openers = list(left.openers)
    closers = list(right.closers)
    cross: list[Mismatch] = []
    i = 0
    while openers and i < len(closers):
        if openers[-1].btype == closers[i].btype:
            openers.pop()
        else:
            cross.append(Mismatch(open=openers[-1], close=closers[i]))
        i += 1
    # left.mismatches all precede right's; cross events interleave with
    # right's internal mismatches by closer position, so merge those two.
    tail = _merge_mismatches(cross, right.mismatches)
    return Summary(
        closers=left.closers + tuple(closers[i:]),
        openers=tuple(openers) + right.openers,
        mismatches=left.mismatches + tail,
    )


def compose_all(summaries: list[Summary] | tuple[Summary, ...]) -> Summary:
    result = EMPTY_SUMMARY
    for summary in summaries:
        result = compose(result, summary)
    return result
