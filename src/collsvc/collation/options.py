"""Collation options bound to an index version.

The options are a frozen, hashable value object. Changing any field MUST
produce a different :class:`IndexVersion` (see ``versioning.py``), because the
stored sort keys would otherwise have been generated under different rules.

ICU strength levels
-------------------
1 PRIMARY    – base letters only (case/accent-insensitive)
2 SECONDARY  – accents significant, case ignored
3 TERTIARY   – accents and case significant (ICU default)
4 QUATERNARY – distinguishes otherwise-ignored punctuation (when shifted)
15 IDENTICAL – everything significant, byte tie-break
"""
from __future__ import annotations

from dataclasses import dataclass

STRENGTH_PRIMARY = 1
STRENGTH_SECONDARY = 2
STRENGTH_TERTIARY = 3
STRENGTH_QUATERNARY = 4
STRENGTH_IDENTICAL = 15

VALID_STRENGTHS = frozenset(
    {
        STRENGTH_PRIMARY,
        STRENGTH_SECONDARY,
        STRENGTH_TERTIARY,
        STRENGTH_QUATERNARY,
        STRENGTH_IDENTICAL,
    }
)

CASE_FIRST_DEFAULT = "default"
CASE_FIRST_UPPER = "upper"
CASE_FIRST_LOWER = "lower"
VALID_CASE_FIRST = frozenset({CASE_FIRST_DEFAULT, CASE_FIRST_UPPER, CASE_FIRST_LOWER})


@dataclass(frozen=True)
class CollationOptions:
    """Immutable collation specification.

    ``normalization`` is forced on: canonical-equivalent strings (NFC/NFD)
    must receive equal sort keys while retaining their own stored text.
    """

    locale: str = "en_US"
    strength: int = STRENGTH_TERTIARY
    numeric: bool = False
    case_first: str = CASE_FIRST_DEFAULT

    normalization: bool = True

    def validate(self) -> None:
        if not isinstance(self.locale, str) or not self.locale.strip():
            raise InvalidCollationOptions("locale must be a non-empty string")
        if self.strength not in VALID_STRENGTHS:
            raise InvalidCollationOptions(
                f"strength must be one of {sorted(VALID_STRENGTHS)}, "
                f"got {self.strength!r}"
            )
        if self.case_first not in VALID_CASE_FIRST:
            raise InvalidCollationOptions(
                f"case_first must be one of {sorted(VALID_CASE_FIRST)}, "
                f"got {self.case_first!r}"
            )
        if not self.normalization:
            raise InvalidCollationOptions(
                "normalization must stay enabled so canonical-equivalent "
                "strings share a sort key"
            )

    def canonical_dict(self) -> dict[str, object]:
        """Deterministic serialization used as version-hash input."""
        return {
            "locale": self.locale,
            "strength": self.strength,
            "numeric": self.numeric,
            "case_first": self.case_first,
            "normalization": self.normalization,
        }


class InvalidCollationOptions(ValueError):
    """Raised when requested collation options are outside the supported set."""
