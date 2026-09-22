"""
Local text-analysis metrics — algorithm version 1.0.0.

Everything in this module is a deterministic, locally computed statistic.
There is NO model that classifies authorship or AI provenance, and the API
deliberately never returns a verdict such as "AI generated" / "cheating".
Numbers are *leads* for a human reviewer only.

Metrics
=======

1. Paragraph length
   - paragraph_count, mean/median/stdev sentence count per paragraph
   - mean/median sentence length (tokens, words)
   - mean paragraph length (chars)

2. Lexical richness
   - type_token_ratio  = V / N  (distinct word types / total word tokens)
   - mtld             = McCarthy & Jarvis (2010) Measure of Textual
                        Lexical Diversity with factor threshold 0.720.
                        MTLD = mean(forward_MTLD, backward_MTLD), where
                        each factor = tokens / (1 - TTR) accumulated while
                        TTR <= 0.720, and the final partial factor is
                        proportionally counted.
   - honore_stat      = 100 * ln(N) / (1 - V1/V), with V1 hapax
                        legomena (Honoré 1979). The degenerate V1==V case
                        (all types used once, normally tiny texts) is null.

3. Repeated fragments
   For token n-gram sizes n in (8, 12, 16) (word tokens, lowercase,
   alphanumeric only), count n-grams occurring >= 2 times:
   - repeated_ngram_{n}: number of distinct repeated n-grams
   - repeated_token_share: fraction of tokens covered by repeated spans
   - top_fragments: up to 5 human-readable repeated strings

4. Style similarity
   Each document yields a style vector = normalised frequencies of
     (a) a fixed set of English function words, and
     (b) coarse POS tags (content/function split by UD tags),
   both L2-normalised before concatenation so the two halves contribute
   equally. Cosine similarity is computed against every previously stored
   result in the SAME course whose input digest differs (>=3 such
   references required; otherwise the metric is "insufficient_samples").

Small-sample handling
======================
- Fewer than 2 sentences  -> sentence-length stats reported as null,
                             a warning emitted; richness still computed.
- Fewer than 20 word tokens -> TTR/MTLD/Honoré emitted but flagged
                             ("small_sample": true), since they are unstable.
- Fewer than STYLE_MIN_REFERENCE_SAMPLES other analysed course documents ->
                             style_similarity.status = "insufficient_samples".
"""
from __future__ import annotations

import math
import statistics
from collections import Counter
from typing import Iterable

from .nlp import nltk_sentence_tokenizer, spacy_nlp

ALGORITHM_VERSION = "1.0.0"
MTLD_THRESHOLD = 0.720
REPEAT_NGRAM_SIZES = (8, 12, 16)
TOP_FRAGMENT_LIMIT = 5
MIN_WORDS_FOR_RICHNESS = 20

# A closed-class function-word set (frequency-ish, but a fixed vocabulary
# keeps vectors comparable across runs and avoids a stopword download).
FUNCTION_WORDS = [
    "the", "a", "an", "and", "or", "but", "if", "because", "while", "although",
    "of", "to", "in", "on", "at", "by", "for", "with", "about", "against",
    "between", "into", "through", "during", "before", "after", "above", "below",
    "from", "up", "down", "out", "off", "over", "under", "again", "further",
    "then", "once", "here", "there", "when", "where", "why", "how", "all", "any",
    "both", "each", "few", "more", "most", "other", "some", "such", "no", "not",
    "only", "own", "same", "so", "than", "too", "very", "can", "will", "just",
    "is", "am", "are", "was", "were", "be", "been", "being", "have", "has",
    "had", "do", "does", "did", "would", "could", "should", "may", "might",
    "must", "shall", "i", "you", "he", "she", "it", "we", "they", "me", "him",
    "her", "us", "them", "my", "your", "his", "its", "our", "their", "this",
    "that", "these", "those", "what", "which", "who", "whom", "as", "also",
]
FUNCTION_WORD_INDEX = {w: i for i, w in enumerate(FUNCTION_WORDS)}

# Coarse POS columns based on spaCy's UD tags.
COARSE_POS = ["ADJ", "ADP", "ADV", "AUX", "CCONJ", "DET", "NOUN", "NUM",
              "PART", "PRON", "PROPN", "PUNCT", "SCONJ", "VERB", "X"]
POS_INDEX = {tag: i for i, tag in enumerate(COARSE_POS)}

DISCLAIMER = (
    "These metrics are heuristic leads for human review only. "
    "They do not establish authorship and must not be presented as a "
    "determination that text was AI-generated or that cheating occurred."
)


# ---------------------------------------------------------------------------
# Tokenisation helpers
# ---------------------------------------------------------------------------

def _word_tokens(doc) -> list[str]:
    """Lowercased alphanumeric token texts (for TTR / n-grams)."""
    return [t.lower_ for t in doc if t.is_alpha]


def _sentences(doc) -> list:
    return [sent for sent in doc.sents if sent.text.strip()]


# ---------------------------------------------------------------------------
# 1. Paragraph length
# ---------------------------------------------------------------------------

def paragraph_metrics(text: str, doc) -> dict:
    raw_paragraphs = [p for p in text.split("\n\n") if p.strip()]
    if not raw_paragraphs:
        raw_paragraphs = [p for p in text.split("\n") if p.strip()]
    sentences = _sentences(doc)
    words = _word_tokens(doc)

    # Sentence counts per paragraph: attribute each sentence to the paragraph
    # whose character span contains the sentence midpoint.
    sent_counts = [0] * len(raw_paragraphs)
    spans: list[tuple[int, int]] = []
    cursor = 0
    for p in raw_paragraphs:
        start = text.find(p, cursor)
        end = start + len(p)
        spans.append((start, end))
        cursor = end
    for sent in sentences:
        mid = (sent.start_char + sent.end_char) // 2
        for i, (a, b) in enumerate(spans):
            if a <= mid <= b:
                sent_counts[i] += 1
                break

    sent_lengths = [len([t for t in s if t.is_alpha]) for s in sentences]
    para_char_lengths = [len(p) for p in raw_paragraphs]

    result = {
        "paragraph_count": len(raw_paragraphs),
        "sentence_count": len(sentences),
        "word_count": len(words),
        "sentences_per_paragraph": _stats(sent_counts),
        "words_per_sentence": _stats(sent_lengths) if len(sentences) >= 2 else None,
        "chars_per_paragraph": _stats(para_char_lengths),
    }
    return result


def _stats(values: list[int]) -> dict:
    if not values:
        return {"mean": None, "median": None, "stdev": None, "min": None, "max": None}
    mean = statistics.fmean(values)
    return {
        "mean": round(mean, 3),
        "median": statistics.median(values),
        "stdev": round(statistics.pstdev(values), 3) if len(values) > 1 else 0.0,
        "min": min(values),
        "max": max(values),
    }


# ---------------------------------------------------------------------------
# 2. Lexical richness
# ---------------------------------------------------------------------------

def lexical_richness(tokens: list[str]) -> dict:
    n = len(tokens)
    types = set(tokens)
    v = len(types)
    counts = Counter(tokens)
    v1 = sum(1 for c in counts.values() if c == 1)

    ttr = (v / n) if n else None
    # Honoré's statistic (1979): 100 * ln(N) / (1 - V1/V). The degenerate
    # V1==V case means hapax cover all types (very short text) -> null.
    if n and v and (1 - v1 / v) > 0:
        honore = 100 * math.log(n) / (1 - v1 / v)
    else:
        honore = None
    mtld_value = _mtld(tokens) if n else None

    return {
        "tokens": n,
        "types": v,
        "type_token_ratio": round(ttr, 4) if ttr is not None else None,
        "mtld": round(mtld_value, 2) if mtld_value is not None else None,
        "honore_stat": round(honore, 2) if honore is not None else None,
        "hapax_count": v1,
        "small_sample": n < MIN_WORDS_FOR_RICHNESS,
        "mtld_threshold": MTLD_THRESHOLD,
    }


def _mtld_factor_count(tokens: list[str], threshold: float) -> float:
    """One-directional MTLD factor count (McCarthy & Jarvis 2010)."""
    factors = 0.0
    token_count = 0
    types: Counter = Counter()
    for tok in tokens:
        token_count += 1
        types[tok] += 1
        ttr = len(types) / token_count
        if ttr <= threshold:
            factors += 1
            token_count = 0
            types = Counter()
    if token_count > 0:
        # Proportional partial factor (fraction of the distance from TTR=1
        # down to the threshold still to travel).
        current_ttr = len(types) / token_count
        factors += (1 - current_ttr) / (1 - threshold)
    return factors


def _mtld(tokens: list[str]) -> float | None:
    if not tokens:
        return None
    forward = _mtld_factor_count(tokens, MTLD_THRESHOLD)
    backward = _mtld_factor_count(list(reversed(tokens)), MTLD_THRESHOLD)
    factors = (forward + backward) / 2
    if factors == 0:
        return None
    return len(tokens) / factors


# ---------------------------------------------------------------------------
# 3. Repeated fragments
# ---------------------------------------------------------------------------

def repeated_fragments(tokens: list[str], doc) -> dict:
    """Repeated token n-grams -> how many, and a token-coverage fraction."""
    result = {"per_ngram": {}, "top_fragments": []}
    covered: set[int] = set()
    # (n, occurrences, first_position, readable text) — longest fragments
    # first give a reviewer the most surrounding context.
    top: list[tuple[int, int, int, str]] = []

    # Precompute orth tokens with positions, restricted to alphabetic words,
    # using the index into `tokens` (already alpha-only).
    for n in REPEAT_NGRAM_SIZES:
        if len(tokens) < n:
            result["per_ngram"][str(n)] = {
                "repeated_distinct": 0, "repetition_events": 0
            }
            continue
        positions: dict[tuple, list[int]] = {}
        for i in range(len(tokens) - n + 1):
            gram = tuple(tokens[i:i + n])
            positions.setdefault(gram, []).append(i)
        repeated = {g: ps for g, ps in positions.items() if len(ps) > 1}
        events = sum(len(ps) for ps in repeated.values())
        result["per_ngram"][str(n)] = {
            "repeated_distinct": len(repeated),
            "repetition_events": events,
        }
        for gram, ps in repeated.items():
            top.append((n, len(ps), ps[0], " ".join(gram)))
            for start in ps:
                for j in range(start, start + n):
                    covered.add(j)

    result["repeated_token_share"] = (
        round(len(covered) / len(tokens), 4) if tokens else 0.0
    )
    top.sort(key=lambda row: (-row[0], -row[1], row[2]))
    result["top_fragments"] = [
        {"ngram": n, "occurrences": count, "fragment": text}
        for n, count, _pos, text in top[:TOP_FRAGMENT_LIMIT]
    ]
    return result


# ---------------------------------------------------------------------------
# 4. Style vector + similarity
# ---------------------------------------------------------------------------

def build_style_vector(doc) -> list[float]:
    """Concatenated, equal-weight function-word and POS frequency vector."""
    fw = [0.0] * len(FUNCTION_WORDS)
    pos = [0.0] * len(COARSE_POS)
    fw_total = 0
    pos_total = 0
    for token in doc:
        low = token.lower_
        if low in FUNCTION_WORD_INDEX:
            fw[FUNCTION_WORD_INDEX[low]] += 1
            fw_total += 1
        coarse = token.pos_
        if coarse in POS_INDEX:
            pos[POS_INDEX[coarse]] += 1
            pos_total += 1
    fw = _l2_normalise(fw)
    pos = _l2_normalise(pos)
    # Each half is already unit length, so the concatenation gives the two
    # feature families equal weight regardless of vocabulary size.
    return fw + pos


def _l2_normalise(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vec))
    return [v / norm for v in vec] if norm else vec


def cosine_similarity(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        raise ValueError("vector length mismatch")
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def style_similarity(vector: list[float], references: Iterable[dict]) -> dict:
    """
    references: iterable of {"input_sha256", "style_vector", "document_id"}
    for prior results in the same course. Caller excludes same-content
    documents and decides sample sufficiency.
    """
    scored = []
    for ref in references:
        rv = ref.get("style_vector")
        if not rv:
            continue
        score = cosine_similarity(vector, rv)
        scored.append({
            "document_id": ref["document_id"],
            "input_sha256": ref["input_sha256"][:16],
            "cosine": round(score, 4),
        })
    if not scored:
        return {"status": "insufficient_samples", "references": 0, "matches": []}
    scored.sort(key=lambda r: -r["cosine"])
    values = [r["cosine"] for r in scored]
    return {
        "status": "ok",
        "references": len(scored),
        "mean_cosine": round(statistics.fmean(values), 4),
        "max_cosine": round(max(values), 4),
        "matches": scored[:5],
        "note": (
            "High cosine means stylistically similar function-word/POS usage "
            "to an existing sample; it is not evidence of copying or AI use."
        ),
    }


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def analyse_text(text: str, references: Iterable[dict] | None = None,
                 min_reference_samples: int = 3) -> dict:
    """Run the full v1 metric set; returns the JSON-serialisable result body."""
    nlp = spacy_nlp()
    doc = nlp(text)
    tokens = _word_tokens(doc)

    warnings: list[str] = []
    para = paragraph_metrics(text, doc)
    if para["sentence_count"] < 2:
        warnings.append("fewer than 2 sentences detected; sentence statistics are limited")
    richness = lexical_richness(tokens)
    if richness["small_sample"]:
        warnings.append(
            f"fewer than {MIN_WORDS_FOR_RICHNESS} word tokens; richness metrics are unstable"
        )
    repeats = repeated_fragments(tokens, doc)
    vector = build_style_vector(doc)

    refs = list(references or [])
    if len(refs) < min_reference_samples:
        style = {
            "status": "insufficient_samples",
            "references": len(refs),
            "required": min_reference_samples,
            "matches": [],
        }
    else:
        style = style_similarity(vector, refs)

    # NLTK cross-check of sentence boundaries (informational; spaCy is primary).
    nltk_split = nltk_sentence_tokenizer()
    if nltk_split is not None and text.strip():
        try:
            para["sentence_count_nltk"] = len(nltk_split(text))
        except Exception:
            para["sentence_count_nltk"] = None
    else:
        para["sentence_count_nltk"] = None

    return {
        "algorithm_version": ALGORITHM_VERSION,
        "disclaimer": DISCLAIMER,
        "warnings": warnings,
        "paragraphs": para,
        "lexical_richness": richness,
        "repeated_fragments": repeats,
        "style_similarity": style,
        "style_vector": vector,
    }
