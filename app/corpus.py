"""Corpus / lexicon specification.

A :class:`BracketLexicon` declares, once, everything the lexer must treat
specially:

* typed bracket pairs (e.g. ``( )`` as ``paren``, ``[ ]`` as ``square``);
* quote spans (e.g. ``"`` with backslash escape);
* line comments (e.g. ``//``) and block comments (``/* */``).

Critically, bracket characters occurring **inside a quote span are ordinary
text**, so ``"( ]"`` contains no structure at all. Escape rules live on the
quote declaration (``\\`` escapes both the delimiter and itself, optionally a
fixed set of further escape sequences such as ``\\n``).

The module also provides :func:`synthetic_document`, a *deterministic* local
fixture generator used by tests and the demo seeding command. It is fully
independent of the mining kernel, which matters: reference answers must not be
produced by the code under test itself (see ``tests/fixtures/``).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

# ---------------------------------------------------------------------------
# Lexicon declarations
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BracketType:
    """One matched pair of brackets carrying a structural type."""

    name: str
    opening: str
    closing: str

    def __post_init__(self) -> None:
        if len(self.opening) != 1 or len(self.closing) != 1:
            raise ValueError("bracket delimiters must be single characters")
        if self.opening == self.closing:
            raise ValueError("opening and closing delimiters must differ")


@dataclass(frozen=True)
class QuoteSpec:
    """A quoted span.

    ``escape_char`` (if set) escapes any single following character inside the
    span, so ``"a\\"b"`` stays open through the escaped delimiter and
    ``"a\\\\"`` closes at the real quote. Escape handling is declared here, on
    the lexical pattern, rather than guessed by the scanner.
    """

    name: str
    delimiter: str
    escape_char: str | None = "\\"

    def __post_init__(self) -> None:
        if len(self.delimiter) != 1:
            raise ValueError("quote delimiter must be a single character")
        if self.escape_char is not None and len(self.escape_char) != 1:
            raise ValueError("escape character must be a single character")


@dataclass(frozen=True)
class CommentSpec:
    """Optional comment forms; their bodies contain no structure either."""

    name: str
    opening: str
    closing: str | None = None  # None => line comment (to end of line)

    def __post_init__(self) -> None:
        if not self.opening:
            raise ValueError("comment opening must be non-empty")
        if self.closing is not None and not self.closing:
            raise ValueError("comment closing must be non-empty or None")


@dataclass(frozen=True)
class BracketLexicon:
    """Complete lexical pattern declaration for a corpus."""

    brackets: tuple[BracketType, ...]
    quotes: tuple[QuoteSpec, ...] = field(default_factory=tuple)
    comments: tuple[CommentSpec, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        opens: dict[str, str] = {}
        closes: dict[str, str] = {}
        for bt in self.brackets:
            if bt.opening in opens or bt.closing in closes:
                raise ValueError(f"duplicate bracket delimiter in {bt.name}")
            opens[bt.opening] = bt.name
            closes[bt.closing] = bt.name
        if set(opens) & set(closes):
            raise ValueError("a character cannot be both an opener and a closer")
        delims: set[str] = set()
        for q in self.quotes:
            if q.delimiter in opens or q.delimiter in closes:
                raise ValueError("quote delimiter must not be a bracket")
            if q.delimiter in delims:
                raise ValueError(f"duplicate quote delimiter {q.delimiter!r}")
            delims.add(q.delimiter)
        for c in self.comments:
            if c.opening[0] in opens or c.opening[0] in closes:
                raise ValueError("comment opening must not start with a bracket")

    # Convenience lookmaps built once.
    def open_map(self) -> dict[str, tuple[str, str]]:
        """opener char -> (type name, closer char)."""
        return {b.opening: (b.name, b.closing) for b in self.brackets}

    def close_map(self) -> dict[str, tuple[str, str]]:
        """closer char -> (type name, opener char)."""
        return {b.closing: (b.name, b.opening) for b in self.brackets}

    def quote_map(self) -> dict[str, QuoteSpec]:
        return {q.delimiter: q for q in self.quotes}

    def comment_map(self) -> dict[str, CommentSpec]:
        return {c.opening: c for c in self.comments}


def default_lexicon() -> BracketLexicon:
    """The canonical corpus spec used across the service and tests.

    Three bracket types so that cross-type errors like ``( [ ) ]`` are
    possible, double quotes with backslash escape, and ``//`` / ``/* */``
    comments.
    """
    return BracketLexicon(
        brackets=(
            BracketType("paren", "(", ")"),
            BracketType("square", "[", "]"),
            BracketType("brace", "{", "}"),
        ),
        quotes=(QuoteSpec("double", '"', "\\"),),
        comments=(
            CommentSpec("line", "//", None),
            CommentSpec("block", "/*", "*/"),
        ),
    )


# ---------------------------------------------------------------------------
# Deterministic synthetic corpus (local fixtures, no external data)
# ---------------------------------------------------------------------------

# A small alphabet with NO structural characters, guaranteed inert content.
_INERT = "ab cd  ef,gh\tij\nkl;mn:op "


def synthetic_document(
    seed: int = 1,
    *,
    target_length: int = 4096,
    lexicon: BracketLexicon | None = None,
    include_strings: bool = True,
    include_comments: bool = True,
) -> str:
    """Generate a deterministic, *balanced* document of ~target_length.

    The generator builds nested/mixed structures with an explicit open-stack,
    so the result is balanced by construction (an independent mechanism from
    the kernel). A fraction of leaves are quoted strings / comments containing
    bracket characters that must be lexically ignored.

    ``target_length`` is approximate: structure is closed in LIFO order and
    the document ends balanced even if that overruns the target slightly.
    """
    if seed < 0:
        raise ValueError("seed must be non-negative")
    lex = lexicon or default_lexicon()
    rng = _Lcg(seed)
    openers = [b.opening for b in lex.brackets]
    closers = {b.opening: b.closing for b in lex.brackets}
    stack: list[str] = []
    out: list[str] = []

    def add_noise(min_len: int = 0, max_len: int = 12) -> None:
        n = rng.uniform(min_len, max_len)
        for _ in range(n):
            out.append(_INERT[rng.uniform(0, len(_INERT) - 1)])

    while len("".join(out)) < target_length or stack:
        if stack and (
            len("".join(out)) >= target_length
            or rng.uniform(0, 99) < 45
            or len(stack) >= 12
        ):
            opener = stack.pop()
            add_noise(0, 6)
            out.append(closers[opener])
            continue
        add_noise(0, 10)
        roll = rng.uniform(0, 99)
        if include_strings and lex.quotes and roll < 12:
            out.append(_make_string(rng, lex))
        elif include_comments and lex.comments and roll < 20:
            out.append(_make_comment(rng, lex))
        else:
            ch = openers[rng.uniform(0, len(openers) - 1)]
            out.append(ch)
            stack.append(ch)
    return "".join(out)


def _make_string(rng: "_Lcg", lex: BracketLexicon) -> str:
    q = lex.quotes[rng.uniform(0, len(lex.quotes) - 1)]
    body_chars = list(_INERT) + [b.opening for b in lex.brackets] + [
        b.closing for b in lex.brackets
    ]
    parts = [q.delimiter]
    for _ in range(rng.uniform(2, 10)):
        if q.escape_char and rng.uniform(0, 99) < 25:
            # An escape followed by a delimiter or the escape char itself:
            # these are the exact sequences a naive scanner mishandles.
            parts.append(q.escape_char)
            parts.append(
                q.delimiter if rng.uniform(0, 1) else q.escape_char
            )
        else:
            parts.append(body_chars[rng.uniform(0, len(body_chars) - 1)])
    parts.append(q.delimiter)
    return "".join(parts)


def _make_comment(rng: "_Lcg", lex: BracketLexicon) -> str:
    c = lex.comments[rng.uniform(0, len(lex.comments) - 1)]
    junk = list(_INERT) + [b.opening for b in lex.brackets]
    length = rng.uniform(2, 8)
    body = "".join(junk[rng.uniform(0, len(junk) - 1)] for _ in range(length))
    if c.closing is None:
        # Avoid a trailing newline eating the following real structure only
        # when we do not put one; the lexer ends line comments at '\n'.
        return f"{c.opening}{body}\n"
    return f"{c.opening}{body}{c.closing}"


class _Lcg:
    """Tiny deterministic LCG so fixtures need no numpy and never vary."""

    __slots__ = ("state",)

    def __init__(self, seed: int) -> None:
        self.state = (seed + 1) & 0xFFFFFFFF

    def uniform(self, low: int, high: int) -> int:
        # Numerical Recipes constants.
        self.state = (1664525 * self.state + 1013904223) & 0xFFFFFFFF
        return low + (self.state % (high - low + 1))


def iter_bracket_chars(lexicon: BracketLexicon) -> Iterable[str]:
    for b in lexicon.brackets:
        yield b.opening
        yield b.closing
