#!/usr/bin/env python3
"""Regenerate pinned golden expectations straight from the installed ICU.

This script talks ONLY to PyICU — it never imports collsvc — so its output is
an independent reference. It prints JSON; when an ICU upgrade changes sort-key
bytes, paste the new literals into tests/fixtures/golden.py after reviewing the
diff (the index version embeds the ICU version and will move as well).

    PYTHONPATH=src python3 scripts/probe_golden.py
"""
from __future__ import annotations

import json
import unicodedata

import icu

A = icu.UCollAttribute
V = icu.UCollAttributeValue


def collator(locale: str, strength: int, numeric: bool = False,
             case_first: str = "default"):
    c = icu.Collator.createInstance(icu.Locale(locale))
    c.setStrength(strength)
    c.setAttribute(A.NUMERIC_COLLATION, V.ON if numeric else V.OFF)
    c.setAttribute(A.NORMALIZATION_MODE, V.ON)
    c.setAttribute(
        A.CASE_FIRST,
        {"default": V.DEFAULT, "upper": V.UPPER_FIRST,
         "lower": V.LOWER_FIRST}[case_first],
    )
    return c


def main() -> None:
    t3 = collator("en_US", 2)
    cn = collator("en_US", 2, numeric=True)
    co = collator("en_US", 2, numeric=False)
    cote = ["cote", "coté", "côte", "côté"]
    files = ["file10", "file2", "file1", "file02", "file20"]
    case_words = ["a", "A", "aa", "Aa"]
    tr_words = ["sıcak", "şapka", "SİZ", "sağlam", "üç", "öğle",
                "çay", "ağır", "ırmak", "İstanbul"]

    nfc = "café"
    nfd = unicodedata.normalize("NFD", nfc)
    payload = {
        "icu_version": str(icu.ICU_VERSION),
        "unicode_version": str(icu.UNICODE_VERSION),
        "en_tertiary_keys": {w: t3.getSortKey(w).hex() for w in cote},
        "numeric_on_keys": {
            w: cn.getSortKey(w).hex() for w in ["file2", "file02", "file10"]
        },
        "numeric_off_keys": {
            w: co.getSortKey(w).hex() for w in ["file2", "file02", "file10"]
        },
        "order_cote_tertiary": sorted(cote, key=t3.getSortKey),
        "order_file_numeric": sorted(files, key=cn.getSortKey),
        "order_file_plain": sorted(files, key=co.getSortKey),
        "case_default": sorted(case_words, key=collator("en_US", 2).getSortKey),
        "case_upper": sorted(
            case_words, key=collator("en_US", 2, case_first="upper").getSortKey
        ),
        "case_lower": sorted(
            case_words, key=collator("en_US", 2, case_first="lower").getSortKey
        ),
        "order_turkish_tr": sorted(
            tr_words, key=collator("tr_TR", 2).getSortKey
        ),
        "order_turkish_viewed_by_en": sorted(
            tr_words, key=collator("en_US", 2).getSortKey
        ),
        "tr_primary_compare": {
            "id_vs_Id": collator("tr_TR", 0).compare("id", "Id"),
            "id_vs_ıd": collator("tr_TR", 0).compare("id", "ıd"),
            "id_vs_İd": collator("tr_TR", 0).compare("id", "İd"),
        },
        "en_primary_compare": {
            "id_vs_İd": collator("en_US", 0).compare("id", "İd"),
        },
        "cote_primary_distinct": len(
            {collator("en_US", 0).getSortKey(w) for w in cote}
        ),
        "cote_secondary_distinct": len(
            {collator("en_US", 1).getSortKey(w) for w in cote}
        ),
        "nfc_nfd_share_key_en": (
            t3.getSortKey(nfc) == t3.getSortKey(nfd) and nfc != nfd
        ),
        "etude_between_e_and_f": (
            int(t3.compare("étude", "e")) >= 0
            and int(t3.compare("étude", "f")) < 0
        ),
        "utf8_etude_after_f": "étude".encode("utf-8") > "f".encode("utf-8"),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
