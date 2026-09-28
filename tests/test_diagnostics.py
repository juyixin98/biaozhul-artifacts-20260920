"""Diagnostics: request ids, decision records, sensitive-item masking."""
from __future__ import annotations

import logging

from app.diagnostics import (
    get_request_id,
    mask_item,
    mask_items,
    new_request_id,
    set_request_id,
)


def test_request_id_lifecycle():
    rid = new_request_id()
    assert get_request_id() == rid and len(rid) == 12
    set_request_id("fixed-id")
    assert get_request_id() == "fixed-id"


def test_mask_item_hides_raw_value():
    masked = mask_item("patient-123-diagnosis")
    assert "patient" not in masked
    assert masked.startswith("item#")
    # deterministic
    assert mask_item("patient-123-diagnosis") == masked
    # opt-out returns the raw value
    assert mask_item("beer", enabled=False) == "beer"


def test_mask_items_sorted_and_masked():
    out = mask_items({"beer", "milk"})
    assert out == sorted(out)
    assert all(x.startswith("item#") for x in out)


def test_decisions_logged_with_request_id(service, load_corpus, caplog):
    set_request_id("req-diag-1")
    corpus_id = load_corpus("exclusive")
    with caplog.at_level(logging.INFO, logger="audit"):
        service.evaluate(corpus_id, ["alpha"], ["beta"])
    records = [r for r in caplog.records if "req-diag-1" in r.getMessage()]
    assert any("decision=accepted" in r.getMessage() for r in records)


def test_undecidable_logged_for_zero_denominator(service, load_corpus, caplog):
    set_request_id("req-diag-2")
    corpus_id = load_corpus("basic")
    with caplog.at_level(logging.INFO, logger="audit"):
        service.evaluate(corpus_id, ["caviar"], ["beer"])
    messages = [r.getMessage() for r in caplog.records]
    assert any(
        "decision=undecidable" in m and "UNDEFINED_ZERO_DENOMINATOR" in m
        and "req-diag-2" in m
        for m in messages
    )


def test_raw_items_not_logged_when_masking(service, load_corpus, caplog):
    corpus_id = load_corpus("basic")
    with caplog.at_level(logging.INFO, logger="audit"):
        service.evaluate(corpus_id, ["caviar"], ["beer"])
    for record in caplog.records:
        assert "caviar" not in record.getMessage()
