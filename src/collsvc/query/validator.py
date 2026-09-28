"""Independent query-result verification (test oracle).

The oracle deliberately does **not** reuse the index store's engine instance
or any SQL. It constructs a *fresh* ICU collator from the option values and
recomputes the expected answer in plain Python from an explicit list of
``(doc_id, text, seq)`` tuples. The mature sorting library is therefore the
shared source of truth, while every part of the code under test (sort-key
storage, BLOB ordering, seek interval, filtering, pagination) is checked
against an independently written reference.

It also checks structural properties an implementation must never violate:
key monotonicity, the 0x00 terminator, and concrete cases where UTF-8 byte
order would give the wrong answer.
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass

import icu

from ..collation.options import CollationOptions
from ..collation.versioning import VersionMismatchError

_STRENGTH_TO_ICU = {
    1: icu.Collator.PRIMARY,
    2: icu.Collator.SECONDARY,
    3: icu.Collator.TERTIARY,
    4: icu.Collator.QUATERNARY,
    15: icu.Collator.IDENTICAL,
}


@dataclass(frozen=True)
class OracleRow:
    doc_id: str
    text: str
    seq: int


class VerificationError(AssertionError):
    """Observed result disagrees with the independent oracle."""


class IndependentOracle:
    def __init__(self, options: CollationOptions, expected_version: str) -> None:
        options.validate()
        collator = icu.Collator.createInstance(icu.Locale(options.locale))
        collator.setStrength(_STRENGTH_TO_ICU[options.strength])
        attr = icu.UCollAttribute
        val = icu.UCollAttributeValue
        collator.setAttribute(
            attr.NUMERIC_COLLATION, val.ON if options.numeric else val.OFF
        )
        collator.setAttribute(attr.NORMALIZATION_MODE, val.ON)
        collator.setAttribute(
            attr.CASE_FIRST,
            {
                "default": val.DEFAULT,
                "upper": val.UPPER_FIRST,
                "lower": val.LOWER_FIRST,
            }[options.case_first],
        )
        self._collator = collator
        self.options = options
        self.expected_version = expected_version

    def require_version(self, observed_version: str) -> None:
        if observed_version != self.expected_version:
            raise VersionMismatchError(
                f"result carries {observed_version!r}, oracle expects "
                f"{self.expected_version!r}"
            )

    def sort_key(self, text: str) -> bytes:
        return bytes(self._collator.getSortKey(text))

    def _ordered(self, rows: list[OracleRow]) -> list[OracleRow]:
        # Stable (key, seq) ordering — mirrors the SQL ORDER BY sort_key, seq.
        return sorted(rows, key=lambda r: (self.sort_key(r.text), r.seq))

    def expected_sorted(self, rows: list[OracleRow]) -> list[OracleRow]:
        return self._ordered(list(rows))

    def expected_range(
        self, rows: list[OracleRow], low: str, high: str
    ) -> list[OracleRow]:
        lo_key, hi_key = sorted((self.sort_key(low), self.sort_key(high)))
        chosen = [r for r in rows if lo_key <= self.sort_key(r.text) <= hi_key]
        return self._ordered(chosen)

    def collation_head_equal(self, candidate_text: str, prefix: str) -> bool:
        nfc_prefix = unicodedata.normalize("NFC", prefix)
        if not nfc_prefix:
            return True
        nfc_candidate = unicodedata.normalize("NFC", candidate_text)
        head = nfc_candidate[: len(nfc_prefix)]
        if len(head) < len(nfc_prefix):
            return False
        return int(self._collator.compare(prefix, head)) == 0

    def expected_collation_prefix(
        self, rows: list[OracleRow], prefix: str
    ) -> list[OracleRow]:
        chosen = [r for r in rows if self.collation_head_equal(r.text, prefix)]
        return self._ordered(chosen)

    def expected_text_prefix(
        self, rows: list[OracleRow], prefix: str
    ) -> list[OracleRow]:
        nfc_prefix = unicodedata.normalize("NFC", prefix)
        chosen = [
            r
            for r in rows
            if unicodedata.normalize("NFC", r.text).startswith(nfc_prefix)
        ]
        return self._ordered(chosen)

    # ---------- whole-result checks ----------

    def assert_doc_id_order(
        self, observed: list[str], expected: list[OracleRow], what: str
    ) -> None:
        want = [r.doc_id for r in expected]
        if observed != want:
            raise VerificationError(
                f"{what}: observed order {observed} != oracle order {want}"
            )

    # ---------- structural checks ----------

    @staticmethod
    def assert_monotonic(keys: list[bytes]) -> None:
        for prev, curr in zip(keys, keys[1:]):
            if curr < prev:
                raise VerificationError(
                    f"sort keys not monotonic: {prev.hex()} -> {curr.hex()}"
                )

    @staticmethod
    def assert_key_shape(key: bytes) -> None:
        if not key or key[-1] != 0x00:
            raise VerificationError(
                f"sort key must end with 0x00 terminator, got {key.hex()[:32]}"
            )

    @staticmethod
    def assert_utf8_would_misclassify(low: str, inside: str, high: str) -> None:
        """Pin a concrete case where UTF-8 byte order is provably wrong.

        ``inside`` is inside the *collation* interval [low, high] (the caller
        has checked via ICU) but UTF-8 byte comparison must place it outside,
        demonstrating why boundaries use sort keys rather than UTF-8.
        """
        lo_b, in_b, hi_b = (s.encode("utf-8") for s in (low, inside, high))
        if lo_b <= in_b <= hi_b:
            raise VerificationError(
                "expected UTF-8 to misclassify the boundary, but it did not"
            )
