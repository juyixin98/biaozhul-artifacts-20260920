"""Coverage for the remaining API/service paths and rejection branches."""
from __future__ import annotations

import pytest

from app.service import (
    NOT_STRUCTURAL,
    OFFSET_OUT_OF_RANGE,
    DocumentService,
    ServiceError,
)


def _create(client, name, content="()"):
    r = client.post("/documents", json={"name": name, "content": content})
    assert r.status_code == 200, r.text
    return r.json()["data"]


def test_list_and_get_documents(client):
    _create(client, "a", "[]")
    _create(client, "b", "{}")
    listing = client.get("/documents").json()["data"]
    assert {d["name"] for d in listing} >= {"a", "b"}
    one = client.get(f"/documents/{listing[0]['id']}").json()["data"]
    assert one["name"] == listing[0]["name"]


def test_create_duplicate_name_conflict(client):
    _create(client, "dup")
    r = client.post("/documents", json={"name": "dup", "content": ""})
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "NAME_CONFLICT"


def test_get_unknown_document_404(client):
    assert client.get("/documents/9999").status_code == 404


def test_match_out_of_range_and_unknown_doc(client):
    doc = _create(client, "d", "()")
    r = client.get(f"/documents/{doc['id']}/match", params={"offset": 99})
    assert r.status_code == 400
    assert r.json()["detail"]["reason"] == OFFSET_OUT_OF_RANGE
    r2 = client.get("/documents/9999/match", params={"offset": 0})
    assert r2.status_code == 404


def test_analyze_rejects_bad_offset_and_non_structural(client):
    r = client.post("/query/analyze", json={"content": "abc", "offset": 9})
    assert r.status_code == 400
    assert r.json()["detail"]["reason"] == OFFSET_OUT_OF_RANGE
    r2 = client.post("/query/analyze", json={"content": "abc", "offset": 1})
    assert r2.status_code == 400
    assert r2.json()["detail"]["reason"] == NOT_STRUCTURAL


def test_analyze_unterminated_string_offsets_match_undetermined(client):
    # A matched pair and an unterminated quote; the matched pair is still
    # accepted; the document-level verdict is undetermined.
    r = client.post(
        "/query/analyze", json={"content": "() \"x(", "offset": 0}
    )
    body = r.json()
    assert body["status"] == "undetermined"
    assert body["data"]["match_at"]["matched"] is True


def test_edit_unknown_document_404(client):
    r = client.post(
        "/documents/9999/edits",
        json={"start": 0, "end": 0, "replacement": "x", "base_version": 1},
    )
    assert r.status_code == 404


def test_edit_start_after_end_rejected(client):
    doc = _create(client, "d", "abcdef")
    r = client.post(
        f"/documents/{doc['id']}/edits",
        json={"start": 4, "end": 2, "replacement": "x", "base_version": 1},
    )
    assert r.status_code == 400
    assert r.json()["detail"]["reason"] == "start_after_end"


def test_defects_unknown_document_404(client):
    assert client.get("/documents/9999/defects").status_code == 404


def test_verify_unknown_document_404(client):
    assert client.get("/documents/9999/verify").status_code == 404


def test_match_on_defective_bracket_reports_defect(client):
    # Lone opener: querying it returns matched=false with a STRAY_OPEN defect.
    doc = _create(client, "d", "ab(")
    r = client.get(f"/documents/{doc['id']}/match", params={"offset": 2})
    data = r.json()["data"]
    assert data["matched"] is False
    assert data["defect"]["category"] == "STRAY_OPEN"


def test_service_match_at_non_structural_raises(service):
    rec = service.create_document("x", '"["')
    with pytest.raises(ServiceError) as exc:
        service.match_at(rec.id, 2)
    assert exc.value.category == NOT_STRUCTURAL
