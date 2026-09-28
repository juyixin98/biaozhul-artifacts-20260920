"""Corpus specification: token-rule schema plus fixed Unicode/newline semantics.

The character semantics below are FIXED by the corpus specification and are
not configurable per ruleset, so that diagnostics and lexing are reproducible:

- Alphabet: Unicode code points U+0000 .. U+10FFFF.
- Newline mode: ``LF`` — only U+000A is a newline. U+000D is an ordinary char.
- ``.`` matches any code point EXCEPT U+000A.
- ``\\d`` = [0-9], ``\\w`` = [0-9A-Za-z_], ``\\s`` = [U+0009-U+000D, U+0020]
  (these match Python ``re`` with the ``re.ASCII`` flag, which the test oracle
  uses as an independent reference).
- Negated classes (``[^...]``, ``\\D``/``\\W``/``\\S``) are complemented over
  the whole alphabet, so they DO match newline.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator, model_validator

ALPHABET_MAX = 0x10FFFF
NEWLINE_MODE = "LF"
NEWLINE_CODEPOINT = 0x0A

DIGIT_RANGES: tuple[tuple[int, int], ...] = ((0x30, 0x39),)
WORD_RANGES: tuple[tuple[int, int], ...] = (
    (0x30, 0x39),
    (0x41, 0x5A),
    (0x5F, 0x5F),
    (0x61, 0x7A),
)
SPACE_RANGES: tuple[tuple[int, int], ...] = ((0x09, 0x0D), (0x20, 0x20))

RuleName = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")


def normalize_ranges(ranges: tuple[tuple[int, int], ...] | list[tuple[int, int]]) -> tuple[tuple[int, int], ...]:
    """Sort and merge overlapping/adjacent ranges into canonical form."""
    out: list[list[int]] = []
    for lo, hi in sorted(ranges):
        if lo > hi:
            continue
        if out and lo <= out[-1][1] + 1:
            out[-1][1] = max(out[-1][1], hi)
        else:
            out.append([lo, hi])
    return tuple((lo, hi) for lo, hi in out)


def complement_ranges(ranges: tuple[tuple[int, int], ...]) -> tuple[tuple[int, int], ...]:
    """Complement of ``ranges`` over the whole alphabet [0, ALPHABET_MAX]."""
    out: list[tuple[int, int]] = []
    nxt = 0
    for lo, hi in normalize_ranges(ranges):
        if lo > nxt:
            out.append((nxt, lo - 1))
        nxt = max(nxt, hi + 1)
    if nxt <= ALPHABET_MAX:
        out.append((nxt, ALPHABET_MAX))
    return tuple(out)


DOT_RANGES: tuple[tuple[int, int], ...] = complement_ranges(
    ((NEWLINE_CODEPOINT, NEWLINE_CODEPOINT),)
)


class RuleSpec(BaseModel):
    """One token rule: a name, a regex pattern and an explicit priority.

    Lower ``priority`` wins when two rules match the same longest length.
    When omitted, the declaration index is used. Ties in both priority and
    (implicitly) declaration order are impossible because the declaration
    index is the final tie-breaker.
    """

    name: str = RuleName
    pattern: str = Field(min_length=1)
    priority: int | None = Field(default=None, ge=0)


class RuleSetSpec(BaseModel):
    """A named corpus of token rules."""

    name: str = Field(min_length=1, max_length=128)
    rules: list[RuleSpec] = Field(min_length=1)

    @field_validator("name")
    @classmethod
    def _name_chars(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("ruleset name must not be blank")
        return value

    @model_validator(mode="after")
    def _unique_rule_names(self) -> "RuleSetSpec":
        seen: set[str] = set()
        for rule in self.rules:
            if rule.name in seen:
                raise ValueError(f"duplicate rule name {rule.name!r}")
            seen.add(rule.name)
        return self
