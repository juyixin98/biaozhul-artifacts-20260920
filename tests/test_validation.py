"""Validation failures must surface as typed categories, not generic 400s."""

import base64

import pytest

from app.config import Settings
from app.corpus import decode_document, validate_documents, Document
from app.errors import AppError, FailureCategory
from app.validation import resolve_max_candidates, validate_min_docs


def _category(exc_info) -> FailureCategory:
    return exc_info.value.category


def test_invalid_base64_rejected():
    with pytest.raises(AppError) as exc_info:
        decode_document("doc", "!!!not-base64!!!")
    assert _category(exc_info) is FailureCategory.INVALID_BASE64


def test_empty_document_rejected():
    settings = Settings()
    empty = decode_document("doc", base64.b64encode(b"").decode())
    with pytest.raises(AppError) as exc_info:
        validate_documents([empty], settings)
    assert _category(exc_info) is FailureCategory.EMPTY_DOCUMENT


def test_duplicate_document_ids_rejected():
    settings = Settings()
    docs = [Document("same", b"a"), Document("same", b"b")]
    with pytest.raises(AppError) as exc_info:
        validate_documents(docs, settings)
    assert _category(exc_info) is FailureCategory.DUPLICATE_DOC_ID


def test_min_docs_bounds():
    validate_min_docs(1, 3)
    validate_min_docs(3, 3)
    with pytest.raises(AppError) as exc_info:
        validate_min_docs(0, 3)
    assert _category(exc_info) is FailureCategory.INVALID_MIN_DOCS
    with pytest.raises(AppError) as exc_info:
        validate_min_docs(4, 3)
    assert _category(exc_info) is FailureCategory.INVALID_MIN_DOCS


def test_max_candidates_bounds():
    settings = Settings(max_candidates_cap=8, default_max_candidates=4)
    assert resolve_max_candidates(None, settings) == 4
    assert resolve_max_candidates(8, settings) == 8
    with pytest.raises(AppError) as exc_info:
        resolve_max_candidates(0, settings)
    assert _category(exc_info) is FailureCategory.INVALID_MAX_CANDIDATES
    with pytest.raises(AppError) as exc_info:
        resolve_max_candidates(9, settings)
    assert _category(exc_info) is FailureCategory.INVALID_MAX_CANDIDATES
