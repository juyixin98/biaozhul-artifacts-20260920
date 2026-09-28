"""Synthetic corpus fixtures with hand-computed reference answers.

Every expectation below (token lists, pair maps, intervals, categories) was
derived by hand from the lexical spec, NOT by running the kernel. Tests load
these cases and assert the kernel, the oracle, and the chunked index all
reproduce them — so the reference answers are independent of the code under
test.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..kernel.oracle import (
    CATEGORY_BALANCED,
    CATEGORY_TYPE_MISMATCH,
    CATEGORY_UNMATCHED_CLOSE,
    CATEGORY_UNMATCHED_OPEN,
)


@dataclass(frozen=True)
class ExpectedToken:
    kind: str  # "open" | "close"
    btype: str
    pos: int


@dataclass(frozen=True)
class ExpectedInterval:
    start: int
    end: int
    category: str


@dataclass(frozen=True)
class CorpusCase:
    name: str
    text: str
    tokens: tuple[ExpectedToken, ...]
    pairs: dict[int, int] = field(default_factory=dict)
    category: str = CATEGORY_BALANCED
    interval: ExpectedInterval | None = None


CASES: tuple[CorpusCase, ...] = (
    CorpusCase(
        name="simple_balanced",
        text="(a)[b]{c}",
        tokens=(
            ExpectedToken("open", "(", 0),
            ExpectedToken("close", "(", 2),
            ExpectedToken("open", "[", 3),
            ExpectedToken("close", "[", 5),
            ExpectedToken("open", "{", 6),
            ExpectedToken("close", "{", 8),
        ),
        pairs={0: 2, 2: 0, 3: 5, 5: 3, 6: 8, 8: 6},
        category=CATEGORY_BALANCED,
        interval=None,
    ),
    CorpusCase(
        name="cross_mismatch_equal_counts",
        # Every bracket type appears exactly once as open and once as close:
        # per-type net counts are all zero, yet the text is NOT balanced.
        text="([)]",
        tokens=(
            ExpectedToken("open", "(", 0),
            ExpectedToken("open", "[", 1),
            ExpectedToken("close", "(", 2),
            ExpectedToken("close", "[", 3),
        ),
        pairs={},
        category=CATEGORY_TYPE_MISMATCH,
        # First defect: ")" at 2 meets "[" at 1 -> interval covers "[)".
        interval=ExpectedInterval(start=1, end=3, category=CATEGORY_TYPE_MISMATCH),
    ),
    CorpusCase(
        name="net_zero_trap",
        # Net count of "(" type is zero, but order is wrong.
        text=")(",
        tokens=(
            ExpectedToken("close", "(", 0),
            ExpectedToken("open", "(", 1),
        ),
        pairs={},
        category=CATEGORY_UNMATCHED_CLOSE,
        interval=ExpectedInterval(start=0, end=1, category=CATEGORY_UNMATCHED_CLOSE),
    ),
    CorpusCase(
        name="string_shields_brackets",
        text='a = "(["; b = ")]"',
        tokens=(),
        pairs={},
        category=CATEGORY_BALANCED,
        interval=None,
    ),
    CorpusCase(
        name="escaped_backslash_then_quote",
        # " \\ " ( ) "  — the \\ is an escaped backslash, so the string ends
        # at the second quote and () are structural; the trailing quote opens
        # an unterminated string.
        text='"\\\\"()"',
        tokens=(
            ExpectedToken("open", "(", 4),
            ExpectedToken("close", "(", 5),
        ),
        pairs={4: 5, 5: 4},
        category=CATEGORY_BALANCED,
        interval=None,
    ),
    CorpusCase(
        name="escaped_quote_keeps_string_open",
        # " \ " ( " — the \" is escaped, the ( is inside the string.
        text='"\\"("',
        tokens=(),
        pairs={},
        category=CATEGORY_BALANCED,
        interval=None,
    ),
    CorpusCase(
        name="single_quoted_string",
        text="x = ']' + y",
        tokens=(),
        pairs={},
        category=CATEGORY_BALANCED,
        interval=None,
    ),
    CorpusCase(
        name="multi_block_nesting",
        text="fn(a, [b, {c: (d)}], e){f[0] = (g)}",
        tokens=(
            ExpectedToken("open", "(", 2),
            ExpectedToken("open", "[", 6),
            ExpectedToken("open", "{", 10),
            ExpectedToken("open", "(", 14),
            ExpectedToken("close", "(", 16),
            ExpectedToken("close", "{", 17),
            ExpectedToken("close", "[", 18),
            ExpectedToken("close", "(", 22),
            ExpectedToken("open", "{", 23),
            ExpectedToken("open", "[", 25),
            ExpectedToken("close", "[", 27),
            ExpectedToken("open", "(", 31),
            ExpectedToken("close", "(", 33),
            ExpectedToken("close", "{", 34),
        ),
        pairs={
            2: 22, 22: 2,
            6: 18, 18: 6,
            10: 17, 17: 10,
            14: 16, 16: 14,
            23: 34, 34: 23,
            25: 27, 27: 25,
            31: 33, 33: 31,
        },
        category=CATEGORY_BALANCED,
        interval=None,
    ),
    CorpusCase(
        name="unterminated_string_swallows_tail",
        text='ok() then "unterminated (rest [of] text',
        tokens=(
            ExpectedToken("open", "(", 2),
            ExpectedToken("close", "(", 3),
        ),
        pairs={2: 3, 3: 2},
        category=CATEGORY_BALANCED,
        interval=None,
    ),
    CorpusCase(
        name="unmatched_open_tail",
        text="(a)(b",
        tokens=(
            ExpectedToken("open", "(", 0),
            ExpectedToken("close", "(", 2),
            ExpectedToken("open", "(", 3),
        ),
        pairs={0: 2, 2: 0},
        category=CATEGORY_UNMATCHED_OPEN,
        interval=ExpectedInterval(start=3, end=4, category=CATEGORY_UNMATCHED_OPEN),
    ),
)

CASES_BY_NAME: dict[str, CorpusCase] = {case.name: case for case in CASES}
