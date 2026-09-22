"""Metric correctness on fixed sample texts (local, deterministic)."""
from __future__ import annotations

import pytest

from analysis.metrics import (
    ALGORITHM_VERSION,
    MTLD_THRESHOLD,
    analyse_text,
    cosine_similarity,
    lexical_richness,
    repeated_fragments,
)
from analysis.nlp import spacy_nlp

SAMPLE = (
    "The committee reviewed the proposal and the budget. "
    "Members argued that the timeline was ambitious but achievable. "
    "A revised draft will circulate before the next meeting.\n\n"
    "Implementation begins in March. Each team owns one workstream, "
    "and progress is reviewed every two weeks. Risks are logged in a "
    "shared register and revisited at each checkpoint."
)


def test_paragraph_stats():
    result = analyse_text(SAMPLE, references=[])
    p = result["paragraphs"]
    assert p["paragraph_count"] == 2
    assert p["sentence_count"] >= 5
    assert p["words_per_sentence"]["mean"] > 3
    assert p["sentences_per_paragraph"]["mean"] >= 2.5
    # spaCy/NLTK sentence counts should agree closely on clean prose.
    assert p["sentence_count_nltk"] == pytest.approx(p["sentence_count"], abs=2)


def test_lexical_richness_bounds_and_values():
    nlp = spacy_nlp()
    tokens = [t.lower_ for t in nlp(SAMPLE) if t.is_alpha]
    rich = lexical_richness(tokens)
    assert 0 < rich["type_token_ratio"] <= 1
    assert rich["tokens"] == len(tokens)
    assert rich["types"] <= rich["tokens"]
    assert rich["mtld"] > 0
    assert rich["mtld_threshold"] == MTLD_THRESHOLD
    assert rich["honore_stat"] is None or rich["honore_stat"] > 0
    assert rich["small_sample"] is False


def test_repetition_detected_for_verbatim_copy():
    repeated_sentence = (
        "The quarterly report highlights revenue growth across all regions "
        "and notes that margins improved for the third consecutive period."
    )
    text = " ".join([repeated_sentence] * 4)
    result = analyse_text(text, references=[])
    rf = result["repeated_fragments"]
    n8 = rf["per_ngram"]["8"]
    assert n8["repeated_distinct"] >= 1
    assert rf["repeated_token_share"] > 0.5
    assert rf["top_fragments"], "expected a human-readable repeated fragment"
    fragments = " ".join(f["fragment"] for f in rf["top_fragments"])
    assert "quarterly report" in fragments


def test_unique_text_has_low_repetition():
    text = (
        "Distinct vocabulary fills every sentence here. Foxes jump quickly "
        "while brown dogs bark loudly at passing bicycles. Children painted "
        "colourful murals beside the old library yesterday morning."
    )
    rf = analyse_text(text, references=[])["repeated_fragments"]
    assert rf["repeated_token_share"] < 0.1


def test_small_sample_flag_and_warnings():
    result = analyse_text("Short text.", references=[])
    assert result["lexical_richness"]["small_sample"] is True
    assert any("fewer than" in w for w in result["warnings"])
    # One sentence -> sentence length stats unavailable but analysis works.
    assert result["paragraphs"]["words_per_sentence"] is None


def test_style_insufficient_samples_below_threshold():
    result = analyse_text(SAMPLE, references=[], min_reference_samples=3)
    style = result["style_similarity"]
    assert style["status"] == "insufficient_samples"
    assert style["required"] == 3
    assert style["references"] == 0


def test_style_similarity_separates_styles():
    formal = analyse_text(SAMPLE, references=[])["style_vector"]
    # Build two other "reference" results by analysing distinct texts and
    # packaging them as stored references would be.
    refs = []
    other_formal_texts = [
        "The board approved the minutes after the secretary read them. "
        "Directors then discussed the audit findings and the reserve policy.",
        "Funding was allocated following the annual planning review, and "
        "the chair thanked the working group for its detailed analysis.",
        "The regulator published updated guidance, which firms must review "
        "before submitting their next compliance statement in October.",
    ]
    for i, t in enumerate(other_formal_texts):
        refs.append({
            "document_id": 100 + i,
            "input_sha256": f"ref{i}" + "0" * 58,
            "style_vector": analyse_text(t, references=[])["style_vector"],
        })

    formal_result = analyse_text(SAMPLE, references=refs, min_reference_samples=3)
    assert formal_result["style_similarity"]["status"] == "ok"
    assert formal_result["style_similarity"]["references"] == 3
    scores_formal = [m["cosine"] for m in formal_result["style_similarity"]["matches"]]

    casual = (
        "uh so yeah basically we went to the mall and it was super crowded "
        "lol I mean like everyone was there you know?? we got boba and then "
        "just walked around for ages it was fun though honestly"
    )
    casual_result = analyse_text(casual, references=refs, min_reference_samples=3)
    scores_casual = [m["cosine"] for m in casual_result["style_similarity"]["matches"]]

    # Formal prose should be stylistically closer to formal references than
    # the casual transcript is. Metric is a lead, not a classifier.
    assert max(scores_formal) > max(scores_casual)
    # Cosine sanity.
    assert cosine_similarity(formal, formal) == pytest.approx(1.0, abs=1e-9)


def test_algorithm_version_and_disclaimer_present():
    result = analyse_text(SAMPLE, references=[])
    assert result["algorithm_version"] == ALGORITHM_VERSION
    assert "not" in result["disclaimer"].lower()
    assert "ai-generated" in result["disclaimer"].lower()
