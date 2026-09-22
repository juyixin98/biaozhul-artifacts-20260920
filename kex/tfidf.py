"""Deterministic tokenization and TF-IDF for the local search index.

Tokenization rules (documented and stable, no model involved):

1. Latin / ASCII letters and digits are run together into one token and
   lowercased: ``"PostgreSQL 3.6"`` -> ``["postgresql", "3", "6"]`` is NOT
   what we do — instead ``3.6`` is kept together because dots between
   digits are part of the run: -> ``["postgresql", "3.6"]``.
2. CJK characters are segmented by an overlapping 2-character bigram
   window (plus unigrams as recall keys): ``清华大学`` ->
   ``{"清华", "华大", "大学", "清", "华", "大", "学"}``. This is the classic
   local-language shingling approach and needs no dictionary.
3. Every other character (punctuation, whitespace) is a separator.

Weighting:

* raw term frequency ``tf`` = count within the document;
* ``idf = ln((N - df + 0.5) / (df + 0.5) + 1.0)`` (sklearn-style smooth IDF);
* per-posting weight ``w = (1 + ln(tf)) * idf`` (sublinear tf);
* document norm ``sqrt(sum w^2)``;
* query score = cosine similarity = sum(wq * wd) / (norm_q * norm_d).

Stable result order: score descending, then workspace_document_id ascending
— fully deterministic with zero cross-generation reads.
"""
from __future__ import annotations

import math
import re
from collections import Counter

# A latin/digit run keeps internal dots only when surrounded by digits
# (versions like "3.6", "3.12.1").
_LATIN_RUN = re.compile(r"[A-Za-z0-9]+(?:\.\d+)*")
_CJK_CHAR = re.compile(r"[㐀-䶿一-鿿豈-﫿]")


def tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        m = _LATIN_RUN.match(text, i)
        if m:
            tokens.append(m.group(0).lower())
            i = m.end()
            continue
        ch = text[i]
        if _CJK_CHAR.match(ch):
            tokens.append(ch)
            # bigram with next char if that is also CJK
            if i + 1 < n and _CJK_CHAR.match(text[i + 1]):
                tokens.append(ch + text[i + 1])
        i += 1
    return tokens


def term_frequencies(text: str) -> Counter[str]:
    return Counter(tokenize(text))


def sublinear_tf(tf: int) -> float:
    return 1.0 + math.log(tf) if tf > 0 else 0.0


def smooth_idf(n_docs: int, df: int) -> float:
    return math.log((n_docs - df + 0.5) / (df + 0.5) + 1.0)


def cosine_score(
    query_weights: dict[str, float],
    postings_weights: dict[str, float],
    query_norm: float,
    doc_norm: float,
) -> float:
    if query_norm == 0.0 or doc_norm == 0.0:
        return 0.0
    dot = sum(
        wq * postings_weights.get(term, 0.0) for term, wq in query_weights.items()
    )
    return dot / (query_norm * doc_norm)
