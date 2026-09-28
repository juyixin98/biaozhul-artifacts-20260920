"""Query/payload validation: concrete failure categories, not just 'it errors'."""

import pytest

from lcs_batch.config import Settings
from lcs_batch.validation import (
    ErrorCategory,
    LcsError,
    validate_batch,
    validate_document_payloads,
    validate_query_spec,
)

SETTINGS = Settings()


def expect(category: ErrorCategory, fn, *args):
    with pytest.raises(LcsError) as excinfo:
        fn(*args)
    assert excinfo.value.category == category


# -- query spec ------------------------------------------------------------------

def test_min_docs_below_two_rejected():
    expect(ErrorCategory.INVALID_MIN_DOCS, validate_query_spec,
           {"min_docs": 1}, 3, SETTINGS)
    expect(ErrorCategory.INVALID_MIN_DOCS, validate_query_spec,
           {"min_docs": 0}, 3, SETTINGS)
    expect(ErrorCategory.INVALID_MIN_DOCS, validate_query_spec,
           {"min_docs": -2}, 3, SETTINGS)


def test_min_docs_must_be_a_real_int():
    expect(ErrorCategory.INVALID_MIN_DOCS, validate_query_spec,
           {"min_docs": True}, 3, SETTINGS)
    expect(ErrorCategory.INVALID_MIN_DOCS, validate_query_spec,
           {"min_docs": "2"}, 3, SETTINGS)


def test_min_docs_above_corpus_size_rejected():
    expect(ErrorCategory.MIN_DOCS_EXCEEDS_CORPUS, validate_query_spec,
           {"min_docs": 4}, 3, SETTINGS)


def test_max_candidates_bounds():
    expect(ErrorCategory.INVALID_MAX_CANDIDATES, validate_query_spec,
           {"max_candidates": 0}, 3, SETTINGS)
    expect(ErrorCategory.INVALID_MAX_CANDIDATES, validate_query_spec,
           {"max_candidates": SETTINGS.max_candidates_ceiling + 1}, 3, SETTINGS)


def test_valid_spec_uses_defaults():
    spec = validate_query_spec({}, 3, SETTINGS)
    assert spec.min_docs == 2
    assert spec.max_candidates == SETTINGS.default_max_candidates
    assert spec.query_id == "q"


def test_invalid_query_id_rejected():
    expect(ErrorCategory.INVALID_QUERY_ID, validate_query_spec,
           {"query_id": "bad id!"}, 3, SETTINGS)


def test_empty_batch_rejected():
    expect(ErrorCategory.EMPTY_BATCH, validate_batch, [])
    expect(ErrorCategory.EMPTY_BATCH, validate_batch, "not-a-list")


# -- document payloads -------------------------------------------------------------

def _doc(doc_id: str, content: bytes) -> dict:
    import base64

    return {"doc_id": doc_id, "content_b64": base64.b64encode(content).decode()}


def test_empty_corpus_rejected():
    expect(ErrorCategory.EMPTY_CORPUS, validate_document_payloads, [], SETTINGS)
    expect(ErrorCategory.EMPTY_CORPUS, validate_document_payloads, None, SETTINGS)


def test_duplicate_doc_id_rejected():
    expect(ErrorCategory.DUPLICATE_DOC_ID, validate_document_payloads,
           [_doc("a", b"x"), _doc("a", b"y")], SETTINGS)


def test_invalid_doc_id_rejected():
    expect(ErrorCategory.INVALID_DOC_ID, validate_document_payloads,
           [_doc("bad id", b"x")], SETTINGS)
    expect(ErrorCategory.INVALID_DOC_ID, validate_document_payloads,
           [{"doc_id": "", "content_b64": "eA=="}], SETTINGS)


def test_invalid_base64_rejected():
    expect(ErrorCategory.INVALID_CONTENT_ENCODING, validate_document_payloads,
           [{"doc_id": "a", "content_b64": "!!!not-base64!!!"}], SETTINGS)


def test_empty_document_rejected():
    expect(ErrorCategory.EMPTY_DOCUMENT, validate_document_payloads,
           [_doc("a", b"")], SETTINGS)


def test_size_limits_enforced():
    tight = Settings(max_documents=1)
    expect(ErrorCategory.TOO_MANY_DOCUMENTS, validate_document_payloads,
           [_doc("a", b"x"), _doc("b", b"y")], tight)

    tight = Settings(max_document_bytes=2)
    expect(ErrorCategory.DOCUMENT_TOO_LARGE, validate_document_payloads,
           [_doc("a", b"xyz")], tight)

    tight = Settings(max_total_symbols=4)  # 2 docs x (1 byte + 1 separator) = 4
    expect(ErrorCategory.CORPUS_TOO_LARGE, validate_document_payloads,
           [_doc("a", b"xy"), _doc("b", b"yz")], tight)


def test_valid_payload_round_trips_bytes():
    parsed = validate_document_payloads([_doc("a", b"\x00\xff")], SETTINGS)
    assert parsed == [("a", b"\x00\xff")]
