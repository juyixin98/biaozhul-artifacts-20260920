"""Diagnostics: request ids, decision explanations and redaction."""
from __future__ import annotations

from typing import Any, List

from app.diagnostics import DiagnosticCollector, redact_itemset
from app.models import RuleQuery, RuleStatus


def test_records_carry_request_id_and_status() -> None:
    diag = DiagnosticCollector(request_id="req-fixed", redact=True)
    diag.add("validate", RuleStatus.ACCEPTED, "within domain", min_confidence=0.0)
    diag.add("generate", RuleStatus.REJECTED, "below threshold", n_rules=3)
    summary = diag.summary()
    assert [r["request_id"] for r in summary] == ["req-fixed", "req-fixed"]
    assert [r["status"] for r in summary] == ["accepted", "rejected"]
    assert summary[1]["key_state"] == {"n_rules": 3}


def test_redacted_view_does_not_contain_item_names() -> None:
    items = ("secret-sku", "pii-category")
    view = redact_itemset(items)
    rendered = repr(view)
    assert "secret-sku" not in rendered
    assert "pii-category" not in rendered
    assert view["size"] == 2
    assert "itemset_sha256_10" in view
    # Stable hash for repeatability.
    assert redact_itemset(items)["itemset_sha256_10"] == view["itemset_sha256_10"]


def test_collector_redacts_itemsets_by_default() -> None:
    diag = DiagnosticCollector(redact=True)
    view: Any = diag.itemset_view(("alpha", "beta"))
    assert "alpha" not in repr(view)
    non_redacted = DiagnosticCollector(redact=False)
    assert non_redacted.itemset_view(("alpha",)) == ["alpha"]


def test_ingest_diagnostics_explain_normalization_and_counts(
    service: Any, raw_transactions: List[List[str]]
) -> None:
    result = service.ingest_dataset(
        "diag-corpus", raw_transactions, min_support=0.2, overwrite=True
    )
    stages = {d["stage"]: d for d in result.diagnostics}
    assert set(stages) == {"ingest", "normalize", "mine_itemsets", "persist"}
    norm_state = stages["normalize"]["key_state"]
    assert norm_state["duplicate_occurrences_collapsed"] == 3
    assert norm_state["blank_item_tokens_removed"] == 1
    assert norm_state["dropped_blank_transactions"] == 1
    assert stages["mine_itemsets"]["key_state"]["n_itemsets"] == result.n_itemsets


def test_audit_diagnostics_explain_accept_reject_and_indeterminate(
    ingested: Any, raw_transactions: List[List[str]]
) -> None:
    service, dataset_id = ingested
    result = service.audit_rules_raw(
        dataset_id, min_confidence=0.9, request_id="req-trace-1"
    )
    assert result.request_id == "req-trace-1"
    stages = [d["stage"] for d in result.diagnostics]
    assert stages == ["validate", "load_itemsets", "generate"]

    generate_record = next(d for d in result.diagnostics if d["stage"] == "generate")
    counts = generate_record["key_state"]["status_counts"]
    assert set(counts).issubset({s.value for s in RuleStatus})
    assert counts.get("rejected", 0) > 0
    # Rejected rules carry concrete reasons.
    rejected = [r for r in result.rules if r.status == RuleStatus.REJECTED]
    assert rejected
    assert all(any("min_confidence" in why for why in r.reasons) for r in rejected)
