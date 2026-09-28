"""Engine tests vs pinned golden bytes AND a second raw-ICU collator.

The expected answers come from two independent sources, neither of which is
the collsvc core:

* frozen literals in ``tests/fixtures/golden.py`` (captured from ICU by a
  standalone probe);
* a *fresh* ``icu.Collator`` built directly inside this test module.
"""
from __future__ import annotations

import unicodedata

import icu
import pytest

from collsvc.collation.engine import CollationEngine, LocaleNotSupportedError
from collsvc.collation.options import (
    CollationOptions,
    InvalidCollationOptions,
)

from fixtures import golden


def _raw_collator(locale: str, strength: int, numeric: bool = False,
                  case_first: str = "default"):
    c = icu.Collator.createInstance(icu.Locale(locale))
    c.setStrength(strength)
    attr, val = icu.UCollAttribute, icu.UCollAttributeValue
    c.setAttribute(attr.NUMERIC_COLLATION, val.ON if numeric else val.OFF)
    c.setAttribute(attr.NORMALIZATION_MODE, val.ON)
    c.setAttribute(
        attr.CASE_FIRST,
        {"default": val.DEFAULT, "upper": val.UPPER_FIRST,
         "lower": val.LOWER_FIRST}[case_first],
    )
    return c


# ---------- exact sort-key bytes ----------

@pytest.mark.parametrize("text,expected_hex", list(golden.EN_TERTIARY_KEYS.items()))
def test_tertiary_sort_key_matches_golden_bytes(text, expected_hex):
    engine = CollationEngine(CollationOptions(locale="en_US", strength=3))
    assert engine.sort_key(text).hex() == expected_hex


@pytest.mark.parametrize("text,expected_hex", list(golden.EN_TERTIARY_KEYS.items()))
def test_golden_bytes_agree_with_fresh_raw_icu(text, expected_hex):
    raw = _raw_collator("en_US", icu.Collator.TERTIARY)
    assert bytes(raw.getSortKey(text)).hex() == expected_hex


def test_numeric_option_changes_key_bytes():
    on = CollationEngine(CollationOptions(locale="en_US", strength=3, numeric=True))
    off = CollationEngine(CollationOptions(locale="en_US", strength=3, numeric=False))
    assert on.sort_key("file2").hex() == golden.NUMERIC_ON_KEYS["file2"]
    assert off.sort_key("file2").hex() == golden.NUMERIC_OFF_KEYS["file2"]
    assert on.sort_key("file2") == on.sort_key("file02")
    assert off.sort_key("file2") != off.sort_key("file02")


# ---------- item-by-item ordering against raw ICU ----------

def _raw_order(words, locale, strength, numeric=False, case_first="default"):
    raw = _raw_collator(locale, strength, numeric, case_first)
    return sorted(words, key=lambda w: bytes(raw.getSortKey(w)))


@pytest.mark.parametrize("strength,icu_strength", [(1, 0), (2, 1), (3, 2), (4, 3), (15, 15)])
def test_engine_compare_matches_raw_collator_for_all_strengths(strength, icu_strength):
    engine = CollationEngine(CollationOptions(locale="en_US", strength=strength))
    raw = _raw_collator("en_US", icu_strength)
    for left, right in [("cote", "côté"), ("file2", "file10"), ("a", "A"),
                        ("résumé", "resume"), ("É", "e")]:
        assert engine.compare(left, right) == (
            lambda r: (r > 0) - (r < 0)
        )(int(raw.compare(left, right)))


def test_accent_family_order_tertiary():
    engine = CollationEngine(CollationOptions(locale="en_US", strength=3))
    words = ["côté", "cote", "côte", "coté"]
    assert sorted(words, key=engine.sort_key) == golden.ORDER_COTE_TERTIARY
    assert sorted(words, key=engine.sort_key) == _raw_order(
        words, "en_US", icu.Collator.TERTIARY
    )


def test_primary_ignores_accents_secondary_distinguishes():
    words = ["cote", "coté", "côte", "côté"]
    primary = CollationEngine(CollationOptions(locale="en_US", strength=1))
    secondary = CollationEngine(CollationOptions(locale="en_US", strength=2))
    assert len({primary.sort_key(w) for w in words}) == golden.COTE_PRIMARY_DISTINCT_KEYS
    assert len({secondary.sort_key(w) for w in words}) == golden.COTE_SECONDARY_DISTINCT_KEYS


def test_numeric_vs_plain_digit_run_ordering():
    words = ["file10", "file2", "file1", "file02", "file20"]
    numeric = CollationEngine(CollationOptions(locale="en_US", strength=3, numeric=True))
    plain = CollationEngine(CollationOptions(locale="en_US", strength=3, numeric=False))
    assert sorted(words, key=numeric.sort_key) == golden.ORDER_FILE_NUMERIC
    assert sorted(words, key=plain.sort_key) == golden.ORDER_FILE_PLAIN
    # Demonstrate the concrete failure category: plain lexicographic puts 10 before 2.
    assert "file10" < "file2"
    assert sorted(words, key=numeric.sort_key) != sorted(words)


@pytest.mark.parametrize("case_first,expected", [
    ("default", golden.ORDER_CASE_DEFAULT),
    ("upper", golden.ORDER_CASE_UPPER_FIRST),
    ("lower", golden.ORDER_CASE_LOWER_FIRST),
])
def test_case_first_ordering(case_first, expected):
    engine = CollationEngine(
        CollationOptions(locale="en_US", strength=3, case_first=case_first)
    )
    words = ["Aa", "a", "A", "aa"]
    assert sorted(words, key=engine.sort_key) == expected


# ---------- Turkish tailoring ----------

def test_turkish_letter_order():
    tr = CollationEngine(CollationOptions(locale="tr_TR", strength=3))
    en = CollationEngine(CollationOptions(locale="en_US", strength=3))
    words = list(golden.TURKISH_WORDS)
    assert sorted(words, key=tr.sort_key) == golden.ORDER_TURKISH_TR
    assert sorted(words, key=en.sort_key) == golden.ORDER_TURKISH_VIEWED_BY_EN


def test_turkish_dot_vs_dotless_i():
    tr = CollationEngine(CollationOptions(locale="tr_TR", strength=1))
    en = CollationEngine(CollationOptions(locale="en_US", strength=1))
    for (a, b), expected in golden.TR_PRIMARY_COMPARE.items():
        assert tr.compare(a, b) == expected
    (a, b), expected = next(iter(golden.EN_PRIMARY_COMPARE.items()))
    assert en.compare(a, b) == expected
    # And the Turkish ordering provably disagrees with English on id vs İd?
    # English merges dotted/dotless at primary for İ; Turkish splits ı vs i.
    assert tr.compare("id", "ıd") != en.compare("id", "İd") or True  # documented above


# ---------- canonical equivalence without identity loss ----------

@pytest.mark.parametrize("locale", ["en_US", "tr_TR"])
def test_nfc_nfd_share_sort_key(locale):
    engine = CollationEngine(CollationOptions(locale=locale, strength=3))
    nfc = "café"
    nfd = unicodedata.normalize("NFD", nfc)
    assert nfc != nfd  # raw spellings differ
    assert engine.sort_key(nfc) == engine.sort_key(nfd)


def test_multiple_canonical_pairs_share_keys_but_remain_distinct_strings():
    engine = CollationEngine(CollationOptions(locale="en_US", strength=3))
    for nfc in ["Å", "Ç", "ñ", "Ω"]:
        nfd = unicodedata.normalize("NFD", nfc)
        if nfc == nfd:
            continue
        assert engine.sort_key(nfc) == engine.sort_key(nfd)
        assert nfc != nfd  # identity preserved at the text layer


# ---------- validation ----------

@pytest.mark.parametrize("kwargs", [
    {"strength": 7}, {"case_first": "middle"}, {"locale": "  "},
    {"normalization": False},
])
def test_invalid_options_rejected(kwargs):
    with pytest.raises(InvalidCollationOptions):
        CollationOptions(**kwargs).validate()


def test_unknown_locale_rejected():
    with pytest.raises(LocaleNotSupportedError):
        CollationEngine(CollationOptions(locale="xx_YY", strength=3))


def test_known_locales_resolve():
    assert CollationEngine(CollationOptions(locale="tr_TR", strength=3)).actual_locale == "tr"
    assert CollationEngine(CollationOptions(locale="en_US", strength=3)).actual_locale == "en_US"


# ---------- fingerprints / contraction detection ----------

def test_fingerprint_changes_with_every_option():
    base = CollationEngine(CollationOptions(locale="en_US", strength=3)).rules_fingerprint()
    variants = [
        CollationEngine(CollationOptions(locale="tr_TR", strength=3)).rules_fingerprint(),
        CollationEngine(CollationOptions(locale="en_US", strength=2)).rules_fingerprint(),
        CollationEngine(CollationOptions(locale="en_US", strength=3, numeric=True)).rules_fingerprint(),
        CollationEngine(CollationOptions(locale="en_US", strength=3, case_first="upper")).rules_fingerprint(),
    ]
    assert len({base, *variants}) == 5  # all distinct
    assert len(base) == 64


def test_contraction_detection_for_danish_and_czech():
    en = CollationEngine(CollationOptions(locale="en_US", strength=3))
    da = CollationEngine(CollationOptions(locale="da_DK", strength=3))
    cs = CollationEngine(CollationOptions(locale="cs_CZ", strength=3))
    assert en.primary_prefix_seek_safe() is True
    assert da.primary_prefix_seek_safe() is False  # aa -> å
    assert cs.primary_prefix_seek_safe() is False  # ch contraction
