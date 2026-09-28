"""接口集成测试：启动构建、分页、范围检索、版本冲突与可解释错误。"""
from __future__ import annotations

import icu
import pytest

from app.config import CollationRules

from .conftest import build_reference_collator, reference_fixture


def test_startup_auto_builds_index(client):
    resp = client.get("/version")
    assert resp.status_code == 200
    body = resp.json()
    assert body["entry_count"] == 21
    assert body["needs_rebuild"] is False
    assert body["index_version"] == body["kernel_version"]
    assert body["icu_version"] == icu.ICU_VERSION


def test_sorted_endpoint_matches_reference_fixture(client):
    """全量排序结果与冻结参考顺序一致（参考由对照库生成，非服务自身）。"""
    fixture = reference_fixture()
    if fixture["icu_version"] != icu.ICU_VERSION:
        pytest.skip(f"冻结参考基于 ICU {fixture['icu_version']}，当前 {icu.ICU_VERSION}")
    resp = client.get("/entries/sorted", params={"limit": 500})
    assert resp.status_code == 200
    body = resp.json()
    got = [e["id"] for e in body["entries"]]
    assert got == fixture["default_rules_order"]


def test_sorted_endpoint_matches_library_itemwise(client):
    """逐项对照：返回的排序键与对照库逐字节一致，顺序与对照库一致。"""
    reference = build_reference_collator(CollationRules())
    resp = client.get("/entries/sorted", params={"limit": 500})
    entries = resp.json()["entries"]
    for entry in entries:
        expected_key = reference.getSortKey(entry["original"]).hex()
        assert entry["sort_key_hex"] == expected_key, entry["id"]
    keys = [bytes.fromhex(e["sort_key_hex"]) for e in entries]
    assert keys == sorted(keys)


def test_pagination_walks_entire_corpus(client):
    seen, cursor = [], None
    while True:
        params = {"limit": 4}
        if cursor:
            params["cursor"] = cursor
        body = client.get("/entries/sorted", params=params).json()
        seen.extend(e["id"] for e in body["entries"])
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert len(seen) == 21
    assert len(set(seen)) == 21


def test_range_endpoint_uses_sort_keys(client):
    """é..z：UTF-8 下是反向区间，排序键下合法且包含 file*/istanbul 等。"""
    resp = client.get("/entries/range", params={"lower": "é", "upper": "z"})
    assert resp.status_code == 200
    ids = {e["id"] for e in resp.json()["entries"]}
    assert {"num-file1", "num-file2", "tr-istanbul", "de-mueller-ascii"} <= ids
    assert "accent-cote" not in ids
    assert "canon-cafe-nfc" not in ids  # 以 'c' 开头，含 é 也在区间外


def test_range_inversion_returns_422_with_category(client):
    resp = client.get("/entries/range", params={"lower": "z", "upper": "é"})
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["category"] == "RANGE_INVERSION"
    assert error["request_id"]


def test_canonical_equivalents_both_served(client):
    nfc = client.get("/entries/canon-cafe-nfc").json()
    nfd = client.get("/entries/canon-cafe-nfd").json()
    assert nfc["original"] != nfd["original"]
    assert nfc["sort_key_hex"] == nfd["sort_key_hex"]
    assert nfc["nfc"] == nfd["nfc"]


def test_missing_entry_returns_404(client):
    resp = client.get("/entries/no-such-id")
    assert resp.status_code == 404
    assert resp.json()["error"]["category"] == "ENTRY_NOT_FOUND"


def test_request_id_echoed_and_generated(client):
    resp = client.get("/health", headers={"X-Request-ID": "req-fixed-1"})
    assert resp.headers["X-Request-ID"] == "req-fixed-1"
    resp2 = client.get("/health")
    assert resp2.headers["X-Request-ID"]


def test_rule_upgrade_conflicts_until_rebuild(make_client):
    """升级规则后：旧索引拒绝服务（409），旧游标不可混入，重建后恢复。"""
    old_rules = CollationRules(numeric=False)
    with make_client(old_rules, db_name="shared.db") as old_client:
        page1 = old_client.get("/entries/sorted", params={"limit": 4}).json()
        old_cursor = page1["next_cursor"]
        old_version = page1["index_version"]
    # 同一数据库，新规则启动：索引版本冲突。
    new_rules = CollationRules(numeric=True)
    with make_client(new_rules, db_name="shared.db") as new_client:
        version = new_client.get("/version").json()
        assert version["needs_rebuild"] is True
        assert version["index_version"] == old_version

        resp = new_client.get("/entries/sorted")
        assert resp.status_code == 409
        assert resp.json()["error"]["category"] == "INDEX_VERSION_CONFLICT"

        # 旧游标同样被拒绝（版本检查先于游标解码）。
        resp = new_client.get("/entries/sorted", params={"cursor": old_cursor})
        assert resp.status_code == 409

        # 重建后恢复服务；旧游标明确报 STALE_CURSOR。
        rebuild = new_client.post("/admin/rebuild")
        assert rebuild.status_code == 200
        new_version = rebuild.json()["index_version"]
        assert new_version != old_version

        resp = new_client.get("/entries/sorted")
        assert resp.status_code == 200
        assert resp.json()["index_version"] == new_version

        resp = new_client.get("/entries/sorted", params={"cursor": old_cursor})
        assert resp.status_code == 409
        assert resp.json()["error"]["category"] == "STALE_CURSOR"


def test_numeric_rules_change_order(make_client):
    """数字排序开关改变 file2/file10 相对顺序（对照库验证）。"""
    with make_client(CollationRules(numeric=False)) as c:
        body = c.get("/entries/sorted", params={"limit": 500}).json()
        ids = [e["id"] for e in body["entries"]]
        assert ids.index("num-file10") < ids.index("num-file2")
    with make_client(CollationRules(numeric=True)) as c:
        body = c.get("/entries/sorted", params={"limit": 500}).json()
        ids = [e["id"] for e in body["entries"]]
        assert ids.index("num-file2") < ids.index("num-file10")


def test_rebuild_reports_equivalence_groups(client):
    body = client.post("/admin/rebuild").json()
    assert body["canonical_equivalence_groups"] == {
        "café": ["canon-cafe-nfc", "canon-cafe-nfd"]
    }
