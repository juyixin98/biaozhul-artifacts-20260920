"""Lazy, process-wide NLP resource loading (spaCy pipeline + NLTK tokenizer).

Everything is local: the spaCy model wheel and NLTK data ship inside the
image (vendor/). No network calls are made at runtime.
"""
from __future__ import annotations

import functools
import logging
import os

logger = logging.getLogger(__name__)

NLP_MODEL_NAME = "en_core_web_sm"


@functools.lru_cache(maxsize=1)
def spacy_nlp():
    """Load the English small pipeline once per worker process."""
    import spacy

    try:
        nlp = spacy.load(NLP_MODEL_NAME, disable=["ner", "lemmatizer"])
    except OSError:
        # Fallback for environments where the model was linked rather than
        # pip-installed; never downloads anything here.
        nlp = spacy.load("en_core_web_sm", disable=["ner", "lemmatizer"])
    # Sentence boundaries: prefer the parser shipped with the small model.
    if "sentencizer" not in nlp.pipe_names and not nlp.has_pipe("parser"):
        nlp.add_pipe("sentencizer")
    logger.info("spaCy pipeline ready: %s", nlp.pipe_names)
    return nlp


@functools.lru_cache(maxsize=1)
def nltk_sentence_tokenizer():
    """Return an NLTK sentence tokenizer; degrade to None if data missing.

    NLTK's punkt is used as an *independent* second sentence splitter for the
    paragraph metrics; spaCy remains the primary splitter.
    """
    try:
        import nltk

        data_dir = os.environ.get("NLTK_DATA")
        if data_dir and data_dir not in nltk.data.path:
            nltk.data.path.insert(0, data_dir)
        return nltk.sent_tokenize
    except Exception as exc:  # pragma: no cover - depends on shipped data
        logger.warning("NLTK punkt unavailable, using spaCy only: %s", exc)
        return None
