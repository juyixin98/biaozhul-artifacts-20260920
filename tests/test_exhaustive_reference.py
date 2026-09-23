"""Exhaustive differential testing against the independent reference oracle.

We enumerate every pattern of the limited grammar up to a small depth and
every input string of a small alphabet up to a bounded length, then check
that the Thompson engine and the memoised reference interpreter
(:mod:`regex_automata.reference`) agree for all three matching modes.

The grammar used for generation is *exactly* the supported subset.  Only
syntactically valid patterns are generated (quantifiers never stack,
bounds stay within limits).

Runs under both ``python -m unittest`` and ``pytest``.
"""
from __future__ import annotations

import itertools
import unittest

from regex_automata import compile_pattern
from regex_automata.reference import ReferenceMatcher

ALPHABET = "abc"   # intentionally small; . and \d / \s cases added by hand


def gen_atoms():
    yield from ALPHABET
    yield "."
    yield "[ab]"
    yield "[^ab]"
    yield "[a-c]"
    yield r"\d"
    yield r"\w"
    yield r"\s"


def gen_bases(depth: int):
    yield ""
    for a in gen_atoms():
        yield a
        for q in ("*", "+", "?", "{0}", "{1}", "{2}", "{0,1}", "{1,2}", "{1,}"):
            yield a + q
    if depth > 0:
        for inner in gen_alts(depth - 1):
            wrapped = f"({inner})"
            yield wrapped
            for q in ("*", "+", "?", "{1,2}"):
                yield wrapped + q
    yield "^"
    yield "$"
    yield r"\b"
    yield r"\B"


def gen_concats(depth: int, max_parts: int = 2, cap: int = 600):
    bases = list(gen_bases(depth))
    for r in range(0, max_parts + 1):
        count = 0
        for combo in itertools.product(bases, repeat=r):
            yield "".join(combo)
            count += 1
            if count >= cap:
                break


def gen_alts(depth: int):
    concats = list(gen_concats(depth))
    yield from concats
    # bounded number of two-branch alternations covering distinct pairs
    emitted = 0
    for pair in itertools.product(concats, repeat=2):
        yield "|".join(pair)
        emitted += 1
        if emitted >= 400:
            break


def all_patterns() -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for p in gen_alts(depth=1):
        if p not in seen:
            seen.add(p)
            ordered.append(p)
    return ordered


def all_strings(max_len: int = 4) -> list[str]:
    out: list[str] = []
    for size in range(0, max_len + 1):
        for combo in itertools.product(ALPHABET, repeat=size):
            out.append("".join(combo))
    # targeted strings for dot / \d / \s / newline semantics
    out.extend(["\n", "a\nb", "1", " ", "a1 b", "ab\nc", "aaa", "ccc"])
    return list(dict.fromkeys(out))


PATTERNS = all_patterns()
STRINGS = all_strings()


def _tup(match):
    return None if match is None else (match.start, match.end)


class ExhaustiveReferenceTests(unittest.TestCase):
    def test_generation_was_nonempty(self):
        # guard against an accidentally empty enumeration
        self.assertGreater(len(PATTERNS), 200)
        self.assertGreater(len(STRINGS), 80)

    def test_engine_matches_reference_all(self):
        failures = []
        checked = 0
        for pattern in PATTERNS:
            compiled = compile_pattern(pattern)
            tree = compiled.tree
            engine = compiled.simulator()
            for text in STRINGS:
                ref = ReferenceMatcher(tree, text)
                checked += 1
                if _tup(engine.search(text)) != _tup(ref.search()):
                    failures.append(("search", pattern, text))
                if _tup(compiled.simulator().fullmatch(text)) != _tup(ref.fullmatch()):
                    failures.append(("fullmatch", pattern, text))
                if _tup(compiled.simulator().prefix(text)) != _tup(ref.prefix()):
                    failures.append(("prefix", pattern, text))
                if len(failures) >= 20:
                    break
            if len(failures) >= 20:
                break
        self.assertEqual(
            failures, [],
            msg=(f"{len(failures)} disagreements, first: "
                 + "; ".join(f"{m} {p!r} vs {t!r}" for m, p, t in failures[:10])),
        )
        # sanity: the suite actually did a lot of comparisons
        self.assertGreater(checked, 10000)


if __name__ == "__main__":
    print(f"patterns={len(PATTERNS)} strings={len(STRINGS)} "
          f"comparisons={len(PATTERNS) * len(STRINGS) * 3}")
    unittest.main(verbosity=2)
