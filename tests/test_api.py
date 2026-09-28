"""API 层：错误语义（契约：异常不统一返回成功）与查询验证。"""

import pytest
from fastapi.testclient import TestClient

from minidfa.api import create_app
from minidfa.config import Settings


@pytest.fixture()
def client(tmp_path):
    app = create_app(Settings(db_path=str(tmp_path / "api.db"), log_level="WARNING"))
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert "version" in resp.json()


def test_build_and_query_flow(client):
    resp = client.post("/automata", json={"name": "dict", "words": ["ape", "apple", "band"]})
    assert resp.status_code == 201
    body = resp.json()
    assert body["word_count"] == 3
    assert body["state_count"] > 0
    assert body["run_id"]

    assert client.get("/automata/dict/contains", params={"word": "apple"}).json()["accepted"]
    assert not client.get("/automata/dict/contains", params={"word": "app"}).json()["accepted"]

    resp = client.get("/automata/dict/prefix_count", params={"prefix": "ap"})
    assert resp.json()["count"] == 2
    resp = client.get("/automata/dict/prefix_count", params={"prefix": "zzz"})
    assert resp.json()["count"] == 0

    stats = client.get("/automata/dict/stats").json()
    assert stats["word_count"] == 3

    assert client.get("/automata").json()["automata"] == ["dict"]


def test_unsorted_input_rejected_422(client):
    resp = client.post("/automata", json={"name": "bad", "words": ["b", "a"]})
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["category"] == "UNSORTED_INPUT"
    assert error["detail"]["index"] == 1


def test_duplicate_rejected_422(client):
    resp = client.post("/automata", json={"name": "bad", "words": ["a", "a"]})
    assert resp.status_code == 422
    assert resp.json()["error"]["category"] == "DUPLICATE_WORD"


def test_normalize_mode_accepts_unsorted(client):
    resp = client.post(
        "/automata", json={"name": "norm", "words": ["b", "a", "b"], "mode": "normalize"}
    )
    assert resp.status_code == 201
    assert resp.json()["word_count"] == 2


def test_unknown_automaton_404_not_success(client):
    resp = client.get("/automata/ghost/contains", params={"word": "a"})
    assert resp.status_code == 404
    assert resp.json()["error"]["category"] == "AUTOMATON_NOT_FOUND"


def test_empty_word_via_api(client):
    client.post("/automata", json={"name": "ew", "words": ["", "a"]})
    assert client.get("/automata/ew/contains", params={"word": ""}).json()["accepted"]
    resp = client.get("/automata/ew/prefix_count", params={"prefix": ""})
    assert resp.json()["count"] == 2


def test_invalid_name_rejected(client):
    resp = client.post("/automata", json={"name": "bad name!", "words": []})
    assert resp.status_code == 422  # pydantic 校验失败
