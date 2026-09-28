"""End-to-end API tests. Expected values are derived in the test itself
(direct bytes.find / set logic on the fixtures), never from the core under test.
"""

import base64


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _create_corpus(client, docs: dict[str, bytes], name: str = "fixture") -> str:
    payload = {
        "name": name,
        "documents": [
            {"doc_id": doc_id, "content_b64": _b64(content)}
            for doc_id, content in docs.items()
        ],
    }
    response = client.post("/corpora", json=payload)
    assert response.status_code == 201, response.text
    return response.json()["corpus_id"]


def _query(client, corpus_id: str, queries: list[dict]) -> dict:
    response = client.post(f"/corpora/{corpus_id}/queries", json={"queries": queries})
    assert response.status_code == 200, response.text
    return response.json()


FIXTURE_DOCS = {
    "alpha": b"the quick brown fox jumps",
    "beta": b"the quick brown dog sleeps",
    "gamma": b"say the quick brown thing",
}


def test_health_reports_versions(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["app_version"]
    assert body["index_version"] >= 1


def test_batch_query_end_to_end(client):
    corpus_id = _create_corpus(client, FIXTURE_DOCS)
    body = _query(
        client,
        corpus_id,
        [
            {"query_id": "all-three", "min_docs": 3},
            {"query_id": "any-two", "min_docs": 2},
            {"query_id": "impossible", "min_docs": 4},
        ],
    )
    assert body["corpus_id"] == corpus_id
    assert body["request_id"]
    assert body["app_version"]
    results = {r["query_id"]: r for r in body["results"]}

    # Expected longest substring common to all three docs, derived directly:
    expected = b"the quick brown "
    for doc in FIXTURE_DOCS.values():
        assert expected in doc
    assert len(expected) == 16
    assert results["all-three"]["status"] == "ok"
    assert results["all-three"]["length"] == 16
    candidate = results["all-three"]["candidates"][0]
    assert base64.b64decode(candidate["substring_b64"]) == expected
    assert candidate["doc_coverage"] == ["alpha", "beta", "gamma"]
    # Offsets verified straight against the fixture bytes.
    for occ in candidate["occurrences"]:
        content = FIXTURE_DOCS[occ["doc_id"]]
        assert content[occ["offset"] : occ["offset"] + 16] == expected
    assert {o["doc_id"] for o in candidate["occurrences"]} == set(FIXTURE_DOCS)

    # min_docs=2: "the quick brown " (16) still wins over any pair-only match.
    assert results["any-two"]["status"] == "ok"
    assert results["any-two"]["length"] == 16

    # min_docs=4 exceeds the corpus: typed per-query failure, batch survives.
    assert results["impossible"]["status"] == "error"
    assert results["impossible"]["failure_category"] == "INVALID_MIN_DOCS"


def test_request_id_is_echoed_and_correlated(client):
    corpus_id = _create_corpus(client, FIXTURE_DOCS)
    response = client.post(
        f"/corpora/{corpus_id}/queries",
        json={"queries": [{"min_docs": 2}]},
        headers={"x-request-id": "review-trace-1"},
    )
    assert response.headers["x-request-id"] == "review-trace-1"
    assert response.json()["request_id"] == "review-trace-1"


def test_unknown_corpus_is_typed_404(client):
    response = client.post(
        "/corpora/nope/queries", json={"queries": [{"min_docs": 1}]}
    )
    assert response.status_code == 404
    assert response.json()["error"]["category"] == "CORPUS_NOT_FOUND"


def test_bad_corpus_payload_categories(client):
    response = client.post(
        "/corpora",
        json={
            "documents": [
                {"doc_id": "x", "content_b64": _b64(b"a")},
                {"doc_id": "x", "content_b64": _b64(b"b")},
            ]
        },
    )
    assert response.status_code == 400
    assert response.json()["error"]["category"] == "DUPLICATE_DOC_ID"

    response = client.post(
        "/corpora",
        json={"documents": [{"doc_id": "x", "content_b64": "@@@"}]},
    )
    assert response.status_code == 400
    assert response.json()["error"]["category"] == "INVALID_BASE64"


def test_no_result_status_is_explicit(client):
    corpus_id = _create_corpus(client, {"one": b"abc", "two": b"xyz"})
    body = _query(client, corpus_id, [{"query_id": "q", "min_docs": 2}])
    result = body["results"][0]
    assert result["status"] == "no_result"
    assert result["length"] == 0
    assert result["detail"]


def test_index_survives_reload_from_disk(client, settings):
    corpus_id = _create_corpus(client, FIXTURE_DOCS)
    # A second app instance over the same DB must answer identically.
    from fastapi.testclient import TestClient

    from app.main import create_app

    with TestClient(create_app(settings)) as client2:
        body = _query(client2, corpus_id, [{"min_docs": 3}])
    assert body["results"][0]["length"] == 16
