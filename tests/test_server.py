"""HTTP 服务端到端测试(标准库 http.client, 真实端口)。"""

from __future__ import annotations

import json
import threading
from http.client import HTTPConnection
from typing import Any

import pytest

from sparse_retrieval.server import (
    IndexStore,
    build_synthetic_store,
    make_server,
    rebuild_from_docs,
)

pytestmark = pytest.mark.integration


@pytest.fixture()
def server_addr() -> Any:
    index, info = build_synthetic_store(
        seed=42, n_docs=120, dim=50, n_queries=2, n_zero_docs=3
    )
    store = IndexStore(index, info)
    httpd = make_server("127.0.0.1", 0, store)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address[:2]
    try:
        yield host, port, store
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _request(
    addr: tuple[str, int], method: str, path: str, body: dict | None = None
) -> tuple[int, dict]:
    conn = HTTPConnection(addr[0], addr[1], timeout=10)
    payload = json.dumps(body).encode() if body is not None else None
    conn.request(method, path, body=payload,
                 headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    data = json.loads(resp.read().decode())
    conn.close()
    return resp.status, data


def test_health_and_root(server_addr: Any) -> None:
    addr = server_addr[:2]
    status, data = _request(addr, "GET", "/health")
    assert status == 200 and data["status"] == "ok"
    assert data["n_docs"] == 120 and data["dim"] == 50

    status, data = _request(addr, "GET", "/")
    assert status == 200 and "endpoints" in data


def test_search_entries_format_with_stats(server_addr: Any) -> None:
    addr = server_addr[:2]
    status, data = _request(
        addr, "POST", "/search",
        {
            "k": 5,
            "query": {
                "dim": 50,
                "entries": [
                    {"index": 0, "value": 1.0},
                    {"index": 0, "value": -0.5},  # 重复维度, 合并为 0.5
                    {"index": 1, "value": -2.0},  # 负权重
                ],
            },
        },
    )
    assert status == 200
    assert len(data["hits"]) == 5
    assert data["query_nnz"] == 2
    ids = [h["doc_id"] for h in data["hits"]]
    assert ids == sorted(ids, key=lambda i: (-{h["doc_id"]: h["score"] for h in data["hits"]}[i], i))
    stats = data["candidates"]
    assert stats["full_scan_docs"] == 120
    assert stats["candidates_scored"] <= 120


def test_search_inline_vector_and_default_dim(server_addr: Any) -> None:
    addr = server_addr[:2]
    # 查询可省略 dim, 回退到索引维度
    status, data = _request(
        addr, "POST", "/search", {"indices": [1, 2], "values": [1.0, 1.0], "k": 3}
    )
    assert status == 200 and len(data["hits"]) == 3


def test_zero_vector_query_returns_lowest_ids(server_addr: Any) -> None:
    addr = server_addr[:2]
    status, data = _request(
        addr, "POST", "/search", {"dim": 50, "entries": [], "k": 4}
    )
    assert status == 200
    assert [h["doc_id"] for h in data["hits"]] == [0, 1, 2, 3]
    assert all(h["score"] == 0.0 for h in data["hits"])


def test_error_handling(server_addr: Any) -> None:
    addr = server_addr[:2]
    # 非法 JSON
    conn = HTTPConnection(addr[0], addr[1], timeout=10)
    conn.request("POST", "/search", body=b"not-json",
                 headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    assert resp.status == 400 and "error" in json.loads(resp.read().decode())
    conn.close()

    # 空请求体
    status, data = _request(addr, "POST", "/search")
    assert status == 400 and "不能为空" in data["error"]

    # JSON 但不是对象
    conn = HTTPConnection(addr[0], addr[1], timeout=10)
    conn.request("POST", "/search", body=b"[1,2]",
                 headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    assert resp.status == 400
    conn.close()

    # 维度越界
    status, data = _request(
        addr, "POST", "/search", {"dim": 50, "entries": [{"index": 99, "value": 1.0}]}
    )
    assert status == 400 and "维度" in data["error"]

    # 非法 k
    status, data = _request(
        addr, "POST", "/search", {"dim": 50, "entries": [], "k": 0}
    )
    assert status == 400

    # query 不是对象
    status, data = _request(addr, "POST", "/search", {"query": [1], "k": 2})
    assert status == 400

    # POST 未知路径
    status, _ = _request(addr, "POST", "/nope", {})
    assert status == 404

    # GET 未知路径
    status, _ = _request(addr, "GET", "/nope")
    assert status == 404


def test_stats_endpoint(server_addr: Any) -> None:
    addr = server_addr[:2]
    status, data = _request(addr, "GET", "/stats")
    assert status == 200
    assert data["n_docs"] == 120 and data["n_terms"] > 0
    assert data["posting_length_min"] >= 1
    assert data["build"]["source"] == "synthetic"


def test_rebuild_with_explicit_docs_and_search(server_addr: Any) -> None:
    addr = server_addr[:2]
    status, data = _request(
        addr, "POST", "/index/rebuild",
        {
            "dim": 4,
            "docs": [
                {"entries": [{"index": 0, "value": 1.0},
                             {"index": 0, "value": 1.0}]},  # 重复 -> 2.0
                {"entries": [{"index": 1, "value": -1.0}]},
                {"entries": []},  # 零向量文档
            ],
        },
    )
    assert status == 200 and data["n_docs"] == 3 and data["dim"] == 4

    status, data = _request(
        addr, "POST", "/search",
        {"dim": 4, "entries": [{"index": 0, "value": 1.0}], "k": 3},
    )
    assert status == 200
    # doc0=1.0; doc1 与查询无公共维度=0.0; doc2 零向量=0.0, 并列按 ID 升序
    assert [h["doc_id"] for h in data["hits"]] == [0, 1, 2]
    assert data["hits"][0]["score"] == pytest.approx(1.0)
    assert data["hits"][1]["score"] == pytest.approx(0.0)
    assert data["hits"][2]["score"] == pytest.approx(0.0)


def test_rebuild_with_synthetic(server_addr: Any) -> None:
    addr = server_addr[:2]
    status, data = _request(
        addr, "POST", "/index/rebuild",
        {"synthetic": {"seed": 7, "n_docs": 30, "dim": 16}},
    )
    assert status == 200 and data["n_docs"] == 30 and data["dim"] == 16


def test_rebuild_validation() -> None:
    with pytest.raises(ValueError):
        rebuild_from_docs({"dim": 3, "docs": []})
    with pytest.raises(ValueError):
        rebuild_from_docs({"docs": [{"entries": []}]})
    with pytest.raises(ValueError):
        rebuild_from_docs({"dim": 3, "docs": ["not-object"]})
    with pytest.raises(ValueError):
        rebuild_from_docs(
            {"dim": 3, "docs": [{"entries": [{"index": 9, "value": 1.0}]}]}
        )


def test_rebuild_synthetic_param_validation(server_addr: Any) -> None:
    addr = server_addr[:2]
    status, data = _request(
        addr, "POST", "/index/rebuild", {"synthetic": "bad"}
    )
    assert status == 400 and "synthetic" in data["error"]


def test_cli_main_starts_and_stops(monkeypatch: pytest.MonkeyPatch) -> None:
    import sparse_retrieval.server as srv

    calls: list[tuple] = []

    class FakeServer:
        def __init__(self, addr: Any, handler: Any) -> None:
            self.server_address = addr
            calls.append(("init", addr))

        def serve_forever(self) -> None:
            calls.append(("serve", None))
            raise KeyboardInterrupt

        def server_close(self) -> None:
            calls.append(("close", None))

    monkeypatch.setattr(srv, "ThreadingHTTPServer", FakeServer)
    rc = srv.main(["--port", "0", "--n-docs", "40", "--dim", "64",
                   "--log-level", "WARNING"])
    assert rc == 0
    assert ("serve", None) in calls and ("close", None) in calls
