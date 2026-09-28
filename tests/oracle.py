"""Independent test oracle — shares NO code with the kernel under test.

The kernel computes overlap witnesses with its own automata pipeline
(regex AST -> Thompson NFA -> range-partitioned DFA -> BFS). This oracle
verifies those witnesses independently:

- membership: Python's ``re`` engine (``re.ASCII`` to match the fixed corpus
  semantics for \\d, \\w, \\s and dot) decides whether the witness string is
  really accepted by both patterns — i.e. whether it lies in the intersection
  of the two pattern languages;
- minimality: every string strictly shorter than the witness, over an
  alphabet covering all characters that occur in the patterns, is enumerated
  exhaustively and must NOT be accepted by both patterns.

The kernel's witness must equal the oracle's canonical witness (shortest,
ties broken lexicographically) exactly.
"""

from __future__ import annotations

import itertools
import re

_EXTRA_CHARS = "ab cd01xyz_.\t/"


def fullmatch(pattern: str, text: str) -> bool:
    return re.fullmatch(pattern, text, flags=re.ASCII) is not None


def alphabet_for(*patterns: str) -> list[str]:
    chars = {c for c in "".join(patterns) if c.isalnum()}
    chars.update(_EXTRA_CHARS)
    return sorted(chars)


def shortest_common(pattern_a: str, pattern_b: str, max_len: int = 8) -> str | None:
    """Shortest string accepted by both patterns (lexicographic tie-break),
    found by exhaustive enumeration with an independent regex engine."""
    alphabet = alphabet_for(pattern_a, pattern_b)
    for length in range(1, max_len + 1):
        for combo in itertools.product(alphabet, repeat=length):
            candidate = "".join(combo)
            if fullmatch(pattern_a, candidate) and fullmatch(pattern_b, candidate):
                return candidate
    return None


def verify_witness(pattern_a: str, pattern_b: str, witness: str) -> list[str]:
    """Return a list of problems with ``witness`` (empty list = verified)."""
    problems: list[str] = []
    if not fullmatch(pattern_a, witness):
        problems.append(f"witness {witness!r} not accepted by {pattern_a!r}")
    if not fullmatch(pattern_b, witness):
        problems.append(f"witness {witness!r} not accepted by {pattern_b!r}")
    expected = shortest_common(pattern_a, pattern_b, max_len=len(witness))
    if expected != witness:
        problems.append(
            f"witness {witness!r} is not the canonical shortest common "
            f"string (oracle found {expected!r})"
        )
    return problems
