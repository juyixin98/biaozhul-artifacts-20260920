"""PyICU collation engine.

Thin, explicit wrapper around a *real* mature sorting library (ICU 74 via
PyICU). It produces:

* binary sort keys (``bytes``) — stored as SQLite BLOBs, byte-order comparable;
* rule fingerprints (tailoring rules + library/Unicode versions) — used by the
  index-version derivation to detect an upstream rule change.

The module never invents ordering itself: every key and comparison comes from
``icu.Collator``.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Final

import icu

from .options import (
    CASE_FIRST_LOWER,
    CASE_FIRST_UPPER,
    CollationOptions,
    STRENGTH_IDENTICAL,
    STRENGTH_PRIMARY,
    STRENGTH_QUATERNARY,
    STRENGTH_SECONDARY,
    STRENGTH_TERTIARY,
)

# Public 1-based strength constants -> ICU enum values.
_STRENGTH_TO_ICU: Final = {
    STRENGTH_PRIMARY: icu.Collator.PRIMARY,
    STRENGTH_SECONDARY: icu.Collator.SECONDARY,
    STRENGTH_TERTIARY: icu.Collator.TERTIARY,
    STRENGTH_QUATERNARY: icu.Collator.QUATERNARY,
    STRENGTH_IDENTICAL: icu.Collator.IDENTICAL,
}

_CASE_FIRST_TO_ICU: Final = {
    "default": icu.UCollAttributeValue.DEFAULT,
    CASE_FIRST_UPPER: icu.UCollAttributeValue.UPPER_FIRST,
    CASE_FIRST_LOWER: icu.UCollAttributeValue.LOWER_FIRST,
}

# ULOC_ACTUAL_LOCALE in ICU's C enum.
_ULOC_ACTUAL_LOCALE: Final = 0
_ULOC_VALID_LOCALE: Final = 1


@dataclass(frozen=True)
class RuntimeInfo:
    icu_version: str
    unicode_version: str

    @staticmethod
    def current() -> "RuntimeInfo":
        return RuntimeInfo(
            icu_version=str(icu.ICU_VERSION),
            unicode_version=str(icu.UNICODE_VERSION),
        )


class LocaleNotSupportedError(ValueError):
    """Requested locale id does not resolve to a real ICU collation locale."""


class CollationEngine:
    """One configured ICU collator plus metadata for fingerprinting."""

    def __init__(self, options: CollationOptions) -> None:
        options.validate()
        self._options = options
        self._runtime = RuntimeInfo.current()
        self._collator = self._build(options)
        self._actual_locale = str(self._collator.getLocale(_ULOC_VALID_LOCALE))
        self._rules = self._collator.getRules()

    @property
    def options(self) -> CollationOptions:
        return self._options

    @property
    def actual_locale(self) -> str:
        return self._actual_locale

    @property
    def rules_text(self) -> str:
        return self._rules

    @property
    def runtime(self) -> RuntimeInfo:
        return self._runtime

    @staticmethod
    def _build(options: CollationOptions) -> "icu.Collator":
        try:
            requested = icu.Locale(options.locale)
            collator = icu.Collator.createInstance(requested)
        except Exception as exc:  # ICU raises icu.ICUError for bad input
            raise LocaleNotSupportedError(
                f"ICU could not create a collator for locale {options.locale!r}: {exc}"
            ) from exc

        # ICU silently falls back to root for unknown locales. Detect that by
        # comparing the resolved valid locale against the request.
        resolved = str(collator.getLocale(_ULOC_VALID_LOCALE))
        wanted = options.locale
        if wanted not in ("root", "") and not _locale_matches(resolved, wanted):
            raise LocaleNotSupportedError(
                f"locale {options.locale!r} resolved to fallback {resolved!r}; "
                "only ICU-resolvable collation locales are accepted"
            )

        collator.setStrength(_STRENGTH_TO_ICU[options.strength])
        attr = icu.UCollAttribute
        value = icu.UCollAttributeValue
        collator.setAttribute(
            attr.NUMERIC_COLLATION, value.ON if options.numeric else value.OFF
        )
        # NORMALIZATION_MODE on is required for NFC/NFD to share keys.
        collator.setAttribute(attr.NORMALIZATION_MODE, value.ON)
        collator.setAttribute(
            attr.CASE_FIRST, _CASE_FIRST_TO_ICU[options.case_first]
        )
        return collator

    def sort_key(self, text: str) -> bytes:
        """ICU binary sort key. Deterministic for the engine's configuration."""
        return bytes(self._collator.getSortKey(text))

    def compare(self, left: str, right: str) -> int:
        """Return -1/0/1 in ICU collation order (for tests/diagnostics)."""
        result = int(self._collator.compare(left, right))
        return (result > 0) - (result < 0)

    def collation_prefix_match(self, candidate: str, prefix: str) -> bool:
        """Library-level collation-prefix predicate.

        True iff the leading ``len(NFC(prefix))`` code points of
        ``NFC(candidate)`` compare **equal** to ``prefix`` under this
        collator. At PRIMARY strength that makes the match accent- and
        case-insensitive; at SECONDARY accents matter; at TERTIARY case
        matters too. Both sides are NFC-normalized first so a combining-mark
        spelling never escapes a precomposed prefix.

        Character-prefix semantics (raw ``str.startswith`` on NFC text) are a
        separate, stricter mode handled by the caller.
        """
        import unicodedata

        nfc_prefix = unicodedata.normalize("NFC", prefix)
        if not nfc_prefix:
            return True
        nfc_candidate = unicodedata.normalize("NFC", candidate)
        head = nfc_candidate[: len(nfc_prefix)]
        if len(head) < len(nfc_prefix):
            return False
        return int(self._collator.compare(prefix, head)) == 0

    def primary_prefix_seek_safe(self) -> bool:
        """Whether a primary-section interval guarantees prefix recall.

        The single-key interval narrows prefix candidates correctly only when
        primary weights concatenate per character. It is unsafe (must use a
        full scan) when anything merges or reorders primary weights:

        * numeric collation (a digit run becomes one weight);
        * IDENTICAL strength (combining-boundary weights expand past the
          simple upper bound);
        * a locale **contraction** (e.g. Danish ``aa``→å, Czech ``ch``),
          detected empirically: for a contraction ``xy`` the primary key of
          ``xy`` does not start with the primary key of ``x``.
        """
        if self._options.numeric:
            return False
        if self._options.strength == 15:
            return False
        return not self._detect_contraction()

    def _detect_contraction(self) -> bool:
        """Probe Latin letter pairs for non-concatenating primary weights."""
        letters = "abcdefghijklmnopqrstuvwxyz"

        def primary(text: str) -> bytes:
            body = bytes(self._collator.getSortKey(text)).rstrip(b"\x00")
            head, sep, _ = body.partition(b"\x01")
            return head if sep else body

        for first in letters:
            base = primary(first)
            if not base:
                continue
            for second in letters:
                if not primary(first + second).startswith(base):
                    return True
        return False

    def rules_fingerprint(self) -> str:
        """Stable hash of everything that could change a sort key.

        Includes ICU/Unicode versions, the resolved locale, the raw tailoring
        rules, and all option values. Any change forces a new index version.
        """
        payload = {
            "engine": "pyicu",
            "icu_version": self._runtime.icu_version,
            "unicode_version": self._runtime.unicode_version,
            "requested_locale": self._options.locale,
            "actual_locale": self._actual_locale,
            "rules_sha256": hashlib.sha256(
                self._rules.encode("utf-8")
            ).hexdigest(),
            "rules_length": len(self._rules),
            "options": self._options.canonical_dict(),
        }
        blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _locale_matches(resolved: str, requested: str) -> bool:
    """Check that ICU resolved the request rather than silently falling back.

    Accepts language-level matches (request ``tr`` resolves to ``tr``) and
    full matches (``tr_TR`` -> ``tr_TR``); rejects root fallback for a
    non-empty request.
    """
    if not resolved:
        return False
    res_parts = resolved.replace("-", "_").split("_")
    req_parts = requested.replace("-", "_").split("_")
    # ICU legitimately resolves a request to its language collation root
    # (tr_TR -> tr); a non-existent language resolves to "" (root), which is
    # already rejected by the ``if not resolved`` guard above.
    return res_parts[0] == req_parts[0]
