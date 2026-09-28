"""语料规范测试：校验失败类别与身份保留。"""
from __future__ import annotations

import pytest

from app.corpus import (
    canonical_equivalence_groups,
    load_corpus,
    validate_entries,
)
from app.errors import ErrorCategory, ServiceError

from .conftest import SAMPLE_CORPUS


def test_duplicate_id_rejected():
    with pytest.raises(ServiceError) as excinfo:
        validate_entries([
            {"id": "a", "text": "x"},
            {"id": "a", "text": "y"},
        ])
    assert excinfo.value.category == ErrorCategory.CORPUS_ERROR
    assert excinfo.value.detail["id"] == "a"


def test_empty_text_rejected():
    with pytest.raises(ServiceError) as excinfo:
        validate_entries([{"id": "a", "text": ""}])
    assert excinfo.value.category == ErrorCategory.CORPUS_ERROR


def test_missing_id_rejected():
    with pytest.raises(ServiceError) as excinfo:
        validate_entries([{"text": "x"}])
    assert excinfo.value.category == ErrorCategory.CORPUS_ERROR


def test_empty_corpus_rejected():
    with pytest.raises(ServiceError) as excinfo:
        validate_entries([])
    assert excinfo.value.category == ErrorCategory.CORPUS_ERROR


def test_canonical_equivalents_keep_distinct_identities():
    """规范等价但原文不同的条目：两个身份都必须保留，原文不丢。"""
    entries = load_corpus(SAMPLE_CORPUS)
    by_id = {e.id: e for e in entries}
    nfc = by_id["canon-cafe-nfc"]
    nfd = by_id["canon-cafe-nfd"]
    assert nfc.text != nfd.text            # 原文不同
    assert nfc.nfc == nfd.nfc              # 规范等价
    assert nfc.id != nfd.id                # 身份不同
    assert len(entries) == len({e.id for e in entries})


def test_equivalence_groups_reported():
    entries = load_corpus(SAMPLE_CORPUS)
    groups = canonical_equivalence_groups(entries)
    assert groups == {"café": ["canon-cafe-nfc", "canon-cafe-nfd"]}
