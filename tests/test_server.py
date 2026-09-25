"""End-to-end tests for the HTTP service (stdlib client against live server)."""

import json
import threading
import urllib.request
import urllib.error

import pytest

from sparse_retrieval.server import RetrievalServer


@pytest.fixture()
def server():
    srv = RetrievalServer("127.0.0.1", 0)  # ephemeral port
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=5)


def _request(srv, method, path, body=None):
    url = f"http://127.0.0.1:{srv.server_port}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_health(server):
    status, body = _request(server, "GET", "/health")
    assert status == 200
    assert body == {"status": "ok"}


def test_ingest_query_delete_roundtrip(server):
    status, body = _request(
        server, "POST", "/documents",
        {"doc_id": 1, "vector": [[1, 1.0], [1, 0.5], [2, -1.0]]},
    )
    assert status == 200
    assert body["duplicates_merged"] == 1
    assert body["nnz"] == 2  # dim 1 merged to 1.5, dim 2 kept negative

    _request(server, "POST", "/documents", {"doc_id": 2, "vector": [[1, 1.0]]})
    _request(server, "POST", "/documents", {"doc_id": 3, "vector": [[9, 1.0]]})

    status, body = _request(server, "POST", "/query", {"vector": [[1, 1.0]], "k": 3})
    assert status == 200
    assert body["num_docs"] == 3
    assert body["candidates_examined"] == 2  # doc 3 pruned
    assert body["results"][0]["doc_id"] == 2  # exact match, cosine 1.0
    assert body["results"][0]["score"] == 1.0
    assert body["results"][-1] == {"doc_id": 3, "score": 0.0}

    status, body = _request(server, "DELETE", "/documents/2")
    assert status == 200 and body == {"removed": True}
    status, body = _request(server, "POST", "/query", {"vector": [[1, 1.0]], "k": 3})
    assert [r["doc_id"] for r in body["results"]] == [1, 3]

    status, body = _request(server, "GET", "/stats")
    assert body == {"num_docs": 2, "num_dimensions": 3}


def test_zero_vector_query_over_http(server):
    _request(server, "POST", "/documents", {"doc_id": 1, "vector": [[1, 1.0]]})
    status, body = _request(server, "POST", "/query", {"vector": [], "k": 5})
    assert status == 200
    assert body["results"] == [{"doc_id": 1, "score": 0.0}]
    assert body["candidates_examined"] == 0


@pytest.mark.parametrize(
    "body",
    [
        {"doc_id": 1, "vector": [[1, "x"]]},        # bad weight
        {"doc_id": 1, "vector": [[-1, 1.0]]},       # negative dim
        {"doc_id": "a", "vector": [[1, 1.0]]},      # bad doc_id
        {"doc_id": 1, "vector": [[1, 1.0, 2.0]]},   # malformed pair
        {"vector": [[1, 1.0]]},                     # missing doc_id
    ],
)
def test_invalid_document_requests_return_400(server, body):
    status, resp = _request(server, "POST", "/documents", body)
    assert status == 400
    assert "error" in resp


def test_invalid_query_requests_return_400(server):
    status, resp = _request(server, "POST", "/query", {"vector": [[1, 1.0]], "k": 0})
    assert status == 400 and "error" in resp
    status, resp = _request(server, "POST", "/query", {"k": 5})
    assert status == 400 and "error" in resp


def test_unknown_path_returns_404(server):
    status, _ = _request(server, "GET", "/nope")
    assert status == 404
