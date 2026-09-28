"""Service-layer tests: explainability, persistence, rejection."""
from __future__ import annotations

import pytest

from .conftest import load_fixture


@pytest.mark.unit
def test_service_response_links_request_id_and_version(service):
    payload = load_fixture("multi_inheritance")
    resp = service.classify(payload, request_id="req-fixed-1", cross_check=False)
    assert resp["request_id"] == "req-fixed-1"
    assert resp["engine_version"].startswith("owl-restricted-")
    # key steps recorded with processing locations
    stages = [(s["stage"], s["location"]) for s in resp["processing_steps"]]
    assert ("parse", "app.language.parse_ontology") in stages
    assert ("classify", "app.kernel.reason") in stages


@pytest.mark.unit
def test_service_separates_failure_category_and_uncertainty(service):
    payload = load_fixture("intersection_disjoint_conflict")
    resp = service.classify(payload, cross_check=False)
    assert resp["consistent"] is False
    assert "ONTOLOGY_INCONSISTENT" in resp["failure_categories"]
    assert "CLASS_UNSATISFIABLE" in resp["failure_categories"]
    assert isinstance(resp["uncertainties"], list)


@pytest.mark.unit
def test_service_cross_check_reports_agreement(service):
    payload = load_fixture("equivalence_ring")
    resp = service.classify(payload)
    assert resp["cross_check"]["agreement"] is True
    assert resp["cross_check"]["oracle_model_count"] >= 1


@pytest.mark.unit
def test_service_rejects_unsupported_construct_with_path(service):
    payload = load_fixture("unsupported_union")
    resp = service.classify(payload)
    assert resp["status"] == "rejected"
    assert resp["error"]["code"] == "UNSUPPORTED_CONSTRUCTOR"
    assert "union" in resp["error"]["path"]
    assert resp["failure_categories"] == ["UNSUPPORTED_CONSTRUCT"]


@pytest.mark.unit
def test_request_persisted_and_retrievable(service, store):
    payload = load_fixture("two_assertions_conflict")
    resp = service.classify(payload, request_id="req-persist-1", cross_check=False)
    record = store.get_request("req-persist-1")
    assert record is not None
    assert record["status"] == "ok"
    assert record["consistent"] == 0
    assert "ONTOLOGY_INCONSISTENT" in record["failure_categories"]
    # steps persisted alongside request
    assert len(record["processing_steps"]) == len(resp["processing_steps"])
    assert record["payload_json"]["axioms"] == payload["axioms"]


@pytest.mark.unit
def test_rejected_request_also_persisted(service, store):
    payload = load_fixture("unsupported_union")
    service.classify(payload, request_id="req-rej-1")
    record = store.get_request("req-rej-1")
    assert record["status"] == "rejected"
    assert record["error_code"] == "UNSUPPORTED_CONSTRUCTOR"
    assert record["consistent"] is None


@pytest.mark.unit
def test_subsumption_endpoint_service(service):
    payload = load_fixture("multi_inheritance")
    yes = service.subsumption_query(
        payload, sub="EmperorPenguin", sup="Swimmer", request_id="req-sub-1"
    )
    assert yes["entailed"] is True
    no = service.subsumption_query(
        payload, sub="Penguin", sup="FlightedBird", request_id="req-sub-2"
    )
    assert no["entailed"] is False
