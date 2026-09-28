"""FastAPI 端到端测试：成功路径与具体错误语义。"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.api


def test_health_reports_version_and_run_id(client_factory) -> None:
    client = client_factory()
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["status"] == "ok"
    assert body["version"]
    assert body["run_id"].startswith("pytest-") or len(body["run_id"]) > 0


def test_stats_not_ready_before_build(client_factory) -> None:
    client = client_factory()
    resp = client.get("/stats")
    assert resp.status_code == 200
    assert resp.json()["ready"] is False


def test_query_before_build_is_503_not_success(client_factory) -> None:
    client = client_factory()
    resp = client.get("/query/membership", params={"word": "cat"})
    assert resp.status_code == 503
    body = resp.json()
    assert body["success"] is False
    assert body["error_code"] == "index_not_ready"


def test_build_from_fixture_and_query(client_factory) -> None:
    client = client_factory()
    build = client.post("/admin/build", json={"fixture": "shared_suffix"})
    assert build.status_code == 200, build.text
    report = build.json()
    assert report["success"] is True
    assert report["word_count"] == 6
    assert report["state_count"] == 10
    assert report["merge_count"] == 4
    assert report["duplicate_count"] == 0

    member = client.get("/query/membership", params={"word": "cats"})
    assert member.status_code == 200
    mbody = member.json()
    assert mbody["member"] is True
    assert mbody["status"] == "member"
    assert mbody["terminal_state_id"] is not None

    non_member = client.get("/query/membership", params={"word": "bird"})
    nbody = non_member.json()
    assert nbody["member"] is False
    assert nbody["status"] == "non_member"
    # 非成员是正常结果，success 仍为 true 但 member=false。
    assert nbody["success"] is True

    prefix = client.get("/query/prefix-count", params={"prefix": "cat"})
    pbody = prefix.json()
    assert pbody["count"] == 2
    assert pbody["reachable"] is True

    unreachable = client.get("/query/prefix-count", params={"prefix": "zz"})
    assert unreachable.json()["count"] == 0
    assert unreachable.json()["reachable"] is False


def test_build_from_words_with_duplicates_reports_counts(client_factory) -> None:
    client = client_factory()
    resp = client.post(
        "/admin/build",
        json={"words": ["able", "able", "ask", "blue"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["input_count"] == 4
    assert body["unique_count"] == 3
    assert body["duplicate_count"] == 1
    assert body["word_count"] == 3


def test_unordered_words_rejected_422_with_code(client_factory) -> None:
    client = client_factory()
    resp = client.post(
        "/admin/build",
        json={"words": ["banana", "apple"]},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["success"] is False
    assert body["error_code"] == "unordered_corpus"
    assert "位置" in body["message"]


def test_unordered_words_can_be_sorted_first(client_factory) -> None:
    client = client_factory()
    resp = client.post(
        "/admin/build",
        json={"words": ["banana", "apple", "cherry"], "sort_first": True},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["sorted_by_service"] is True
    assert body["word_count"] == 3
    assert client.get("/query/membership", params={"word": "apple"}).json()["member"]


def test_empty_word_rejected_then_accepted(client_factory) -> None:
    client = client_factory()
    rejected = client.post("/admin/build", json={"words": [""]})
    assert rejected.status_code == 422
    assert rejected.json()["error_code"] == "empty_word_rejected"

    accepted = client.post(
        "/admin/build",
        json={"words": ["", "a", "ab"], "allow_empty_word": True},
    )
    assert accepted.status_code == 200
    assert accepted.json()["word_count"] == 3
    # 索引允许空词后，查询空词返回成员。
    q = client.get("/query/membership", params={"word": ""})
    assert q.status_code == 200
    assert q.json()["member"] is True


def test_empty_query_rejected_on_non_empty_word_index(client_factory) -> None:
    client = client_factory()
    client.post("/admin/build", json={"fixture": "shared_suffix"})
    resp = client.get("/query/membership", params={"word": ""})
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "empty_query_rejected"


def test_unknown_fixture_404(client_factory) -> None:
    client = client_factory()
    resp = client.post("/admin/build", json={"fixture": "nope"})
    assert resp.status_code == 404
    assert resp.json()["error_code"] == "unknown_fixture"


def test_build_empty_collection_is_valid(client_factory) -> None:
    client = client_factory()
    resp = client.post("/admin/build", json={"words": []})
    assert resp.status_code == 200
    assert resp.json()["word_count"] == 0
    assert resp.json()["state_count"] == 1
    # 空索引上，任何成员判定都为非成员，空前缀计数 0（空词被拒绝）。
    assert client.get("/query/prefix-count", params={"prefix": "a"}).json()["count"] == 0


def test_stats_after_build_reports_metadata(client_factory) -> None:
    client = client_factory()
    client.post("/admin/build", json={"fixture": "prefix_words"})
    stats = client.get("/stats").json()
    assert stats["ready"] is True
    assert stats["word_count"] == 5
    assert stats["source_fixture"] == "prefix_words"
    assert stats["build_version"]


def test_persistence_survives_app_restart_with_autoload(tmp_settings) -> None:
    from fastapi.testclient import TestClient

    from app.api.app import create_app

    with TestClient(create_app(settings=tmp_settings, autoload=False)) as first:
        r = first.post("/admin/build", json={"fixture": "shared_suffix"})
        assert r.status_code == 200

    # 新进程视角：新应用实例自动从同一 SQLite 加载。
    with TestClient(create_app(settings=tmp_settings, autoload=True)) as second:
        stats = second.get("/stats").json()
        assert stats["ready"] is True
        assert stats["word_count"] == 6
        assert second.get("/query/membership", params={"word": "dogs"}).json()["member"]


def test_corrupted_autoload_does_not_crash_service(tmp_settings) -> None:
    import sqlite3

    from fastapi.testclient import TestClient

    from app.api.app import create_app

    # 先建一个合法索引。
    with TestClient(create_app(settings=tmp_settings, autoload=False)) as first:
        first.post("/admin/build", json={"fixture": "shared_suffix"})

    # 在磁盘上注入悬空边。
    conn = sqlite3.connect(str(tmp_settings.db_path))
    try:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute(
            "INSERT INTO edges (source, symbol, target) VALUES (0, 'Q', 9999)"
        )
        conn.commit()
    finally:
        conn.close()

    # 自加载遇损坏应被吞掉以保证启动；此时查询返回 503，而不是假成功。
    with TestClient(create_app(settings=tmp_settings, autoload=True)) as second:
        assert second.get("/stats").json()["ready"] is False
        resp = second.get("/query/membership", params={"word": "cat"})
        assert resp.status_code == 503
        assert resp.json()["success"] is False
