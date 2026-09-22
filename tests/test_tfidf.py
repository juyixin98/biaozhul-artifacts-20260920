"""Tokenizer and TF-IDF determinism tests."""
from __future__ import annotations

import math

from kex.tfidf import cosine_score, smooth_idf, sublinear_tf, term_frequencies, tokenize


def test_latin_lowercased_and_version_kept():
    assert tokenize("PostgreSQL 3.6 rocks") == ["postgresql", "3.6", "rocks"]


def test_cjk_unigrams_and_bigrams():
    toks = tokenize("清华大学")
    # unigrams for recall + overlapping bigrams
    assert set(["清", "华", "大", "学", "清华", "华大", "大学"]) <= set(toks)
    assert len(toks) == 7  # 4 unigrams + 3 bigrams, nothing else


def test_punctuation_is_separator():
    assert tokenize("a,b；c！d") == ["a", "b", "c", "d"]


def test_term_frequency_counts():
    tf = term_frequencies("kafka kafka Kafka 清华 清华")
    assert tf["kafka"] == 3
    assert tf["清华"] == 2


def test_idf_and_weight_shapes():
    assert smooth_idf(10, 10) < smooth_idf(10, 1)
    assert sublinear_tf(4) == 1 + math.log(4)


def test_cosine_self_match_is_one():
    w = {"a": 0.8, "b": 0.6}
    norm = math.sqrt(0.8**2 + 0.6**2)
    assert abs(cosine_score(w, w, norm, norm) - 1.0) < 1e-12
