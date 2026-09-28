"""End-to-end API tests via FastAPI TestClient.

Assertions cover concrete payloads and failure categories, plus the
request-id diagnostics header — not just "the endpoint responds".
"""

from __future__ import annotations

REQUEST_ID_HEADER = "X-Request-ID"


def test_create_and_balance_roundtrip(client):
    created = client.post("/documents", json={"text": "(a)[b]"})
    assert created.status_code == 201
    doc_id = created.json()["doc_id"]
    assert created.json()["version"] == 0
    assert REQUEST_ID_HEADER in created.headers

    balance = client.get(f"/documents/{doc_id}/balance")
    assert balance.status_code == 200
    body = balance.json()
    assert body["balanced"] is True
    assert body["category"] == "BALANCED"
    assert body["interval"] is None


def test_cross_mismatch_interval_via_api(client):
    doc_id = client.post("/documents", json={"text": "([)]"}).json()["doc_id"]
    response = client.get(f"/documents/{doc_id}/unbalanced-interval")
    assert response.status_code == 200
    body = response.json()
    assert body["balanced"] is False
    assert body["interval"] == {"start": 1, "end": 3, "category": "TYPE_MISMATCH"}


def test_match_jump_via_api(client):
    text = "fn(a, [b, {c: (d)}], e){f[0] = (g)}"
    doc_id = client.post("/documents", json={"text": text}).json()["doc_id"]
    matched = client.get(f"/documents/{doc_id}/match", params={"pos": 2})
    assert matched.status_code == 200
    assert matched.json() == {
        "doc_id": doc_id,
        "pos": 2,
        "category": "MATCHED",
        "match_pos": 22,
    }
    # Position 0 ('f') is not a bracket.
    not_bracket = client.get(f"/documents/{doc_id}/match", params={"pos": 0})
    assert not_bracket.status_code == 422
    assert not_bracket.json()["error"]["category"] == "NOT_A_BRACKET"


def test_edit_flow_and_stale_version_conflict(client):
    doc_id = client.post("/documents", json={"text": "a(b)c"}).json()["doc_id"]
    edit = client.post(
        f"/documents/{doc_id}/edits",
        json={"expected_version": 0, "start": 0, "end": 0, "replacement": "PRE"},
    )
    assert edit.status_code == 200
    assert edit.json()["version"] == 1
    assert edit.json()["length"] == len("PREa(b)c")
    assert edit.json()["rescanned_chunks"] < edit.json()["total_chunks"] or (
        edit.json()["total_chunks"] == 1
    )

    stale = client.post(
        f"/documents/{doc_id}/edits",
        json={"expected_version": 0, "start": 0, "end": 0, "replacement": "X"},
    )
    assert stale.status_code == 409
    error = stale.json()["error"]
    assert error["category"] == "STALE_VERSION"
    assert error["request_id"]

    # After the failed edit the document is still the version-1 text.
    match = client.get(f"/documents/{doc_id}/match", params={"pos": 4})
    assert match.json()["match_pos"] == 6


def test_invalid_range_rejected(client):
    doc_id = client.post("/documents", json={"text": "abc"}).json()["doc_id"]
    response = client.post(
        f"/documents/{doc_id}/edits",
        json={"expected_version": 0, "start": 0, "end": 99, "replacement": ""},
    )
    assert response.status_code == 422
    assert response.json()["error"]["category"] == "INVALID_RANGE"


def test_missing_document_is_404(client):
    response = client.get("/documents/9999/balance")
    assert response.status_code == 404
    assert response.json()["error"]["category"] == "DOCUMENT_NOT_FOUND"


def test_request_id_is_echoed_when_provided(client):
    response = client.post(
        "/documents",
        json={"text": "()"},
        headers={REQUEST_ID_HEADER: "req-test-123"},
    )
    assert response.headers[REQUEST_ID_HEADER] == "req-test-123"


def test_get_document_state_after_edits(client):
    doc_id = client.post("/documents", json={"text": "abcdef"}).json()["doc_id"]
    client.post(
        f"/documents/{doc_id}/edits",
        json={"expected_version": 0, "start": 0, "end": 2, "replacement": "XYZQ"},
    )
    state = client.get(f"/documents/{doc_id}")
    assert state.status_code == 200
    assert state.json() == {
        "doc_id": doc_id,
        "version": 1,
        "length": len("XYZQcdef"),
    }


def test_typed_mismatch_close_reports_offending_opener(client):
    # "(" is matched with "]" inside: closer at 3 must be TYPE_MISMATCH and
    # point at the stack-top opener.
    doc_id = client.post("/documents", json={"text": "(a]b]"}).json()["doc_id"]
    first_mismatch = client.get(f"/documents/{doc_id}/match", params={"pos": 2})
    assert first_mismatch.json()["category"] == "TYPE_MISMATCH"
    assert first_mismatch.json()["match_pos"] == 0


def test_diagnostics_never_log_raw_document_text(client, caplog):
    caplog.set_level("INFO", logger="bracket_index")
    secret = "SECRET_TOKEN_123"
    doc_id = client.post("/documents", json={"text": f"({secret})"}).json()["doc_id"]
    client.get(f"/documents/{doc_id}/balance")
    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert secret not in log_text
    assert "decision=accepted" in log_text
