"""Independent reference oracle.

This is deliberately written from scratch and hard-codes the default lexical
rules instead of importing anything from ``app.mining``. The product under
test must not generate its own reference answers: agreement here is
meaningful because the two implementations share no parsing code.

The oracle is a plain character-at-a-time state machine with an explicit
stack. Its mismatch convention (discard both the wrong-typed opener and the
closer) is documented in the product spec; the tests assert BOTH conventions'
outputs through hand-authored expectations.
"""
from __future__ import annotations

from dataclasses import dataclass

_OPEN = {"(": "paren", "[": "square", "{": "brace"}
_CLOSE = {")": ("(", "paren"), "]": ("[", "square"), "}": ("{", "brace")}


@dataclass(frozen=True)
class OracleResult:
    matches: tuple[tuple[int, int, str], ...]
    mismatches: tuple[tuple[int, int, str, str], ...]
    stray_closes: tuple[int, ...]
    stray_opens: tuple[int, ...]
    structural_offsets: tuple[int, ...]


def oracle_scan(text: str) -> OracleResult:
    n = len(text)
    i = 0
    stack: list[tuple[int, str]] = []
    matches: list[tuple[int, int, str]] = []
    mismatches: list[tuple[int, int, str, str]] = []
    stray_closes: list[int] = []
    structural: list[int] = []

    while i < n:
        ch = text[i]
        if text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j == -1 else j + 1
            continue
        if text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j == -1 else j + 2
            continue
        if ch == '"':
            j = i + 1
            while j < n:
                if text[j] == "\\":
                    j += 2  # escape consumes one char (may run past EOF)
                    continue
                if text[j] == '"':
                    j += 1
                    break
                j += 1
            i = j
            continue
        if ch in _OPEN:
            stack.append((i, ch))
            structural.append(i)
        elif ch in _CLOSE:
            want, type_name = _CLOSE[ch]
            structural.append(i)
            if not stack:
                stray_closes.append(i)
            else:
                open_pos, open_ch = stack.pop()
                if open_ch == want:
                    matches.append((open_pos, i, type_name))
                else:
                    mismatches.append((open_pos, i, open_ch, ch))
        i += 1

    return OracleResult(
        matches=tuple(matches),
        mismatches=tuple(mismatches),
        stray_closes=tuple(stray_closes),
        stray_opens=tuple(o for o, _ in stack),
        structural_offsets=tuple(structural),
    )


def oracle_match(text: str, offset: int) -> tuple[int, int] | None:
    for o, c, _ in oracle_scan(text).matches:
        if o == offset:
            return o, c
        if c == offset:
            return o, c
    return None
