"""规则版本隔离、并发重建不混代、回滚不改写历史证据。"""
from __future__ import annotations

import io
import json
import pathlib

FIXTURE_V2 = pathlib.Path(__file__).parent / "fixtures" / "rulepack_v2.json"


def _upload(client, headers, text, name="d.txt"):
    resp = client.post(
        "/api/documents",
        data={"file": (io.BytesIO(text.encode("utf-8")), name)},
        headers=headers,
        content_type="multipart/form-data",
    )
    assert resp.status_code == 201, resp.data
    return resp.get_json()


def _publish_v2(client, admin_headers):
    resp = client.post(
        "/admin/rule-packs",
        data={"file": (io.BytesIO(FIXTURE_V2.read_bytes()), "v2.json")},
        headers=admin_headers,
        content_type="multipart/form-data",
    )
    assert resp.status_code in (200, 201), resp.data
    return resp.get_json()["version"]


def _wait(client, headers, no_pending=True, timeout=15):
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        jobs = client.get("/api/jobs", headers=headers).get_json()["jobs"]
        failed = [j for j in jobs if j["status"] == "failed"]
        assert not failed, f"作业失败: {[(j['kind'], j['error_stage'], j['error']) for j in failed]}"
        if not no_pending or not any(j["status"] in ("pending", "running") for j in jobs):
            return jobs
        time.sleep(0.03)
    raise AssertionError("作业超时未完成")


def test_rule_pack_immutable_and_derived_version(client, admin_headers):
    v2a = _publish_v2(client, admin_headers)
    # 相同内容再次发布 → 同一版本，不产生新包
    v2b = _publish_v2(client, admin_headers)
    assert v2a == v2b
    # 篡改一个字符再发布 → 版本号必然不同
    data = json.loads(FIXTURE_V2.read_text(encoding="utf-8"))
    data["rules"][0]["aliases"]["李明"].append("李老师")
    resp = client.post("/admin/rule-packs", json=data, headers=admin_headers)
    assert resp.status_code in (200, 201)
    assert resp.get_json()["version"] != v2a


def test_version_isolation_entities_kept_per_version(ws, client, drain, admin_headers):
    h = ws["headers"]
    v1 = ws["default_rule"]
    # v1 里没有「周杰」「Rust」
    text = "周工说，新内核用 Rust 编写，李明表示赞同。2025-01-08 发布。"
    up = _upload(client, h, text)
    drain()
    v1_ents = client.get(
        f"/api/documents/{up['doc_id']}/entities?rule_pack_version={v1}", headers=h
    ).get_json()
    v1_persons = {e["canonical"] for e in v1_ents["entities"] if e["entity_type"] == "person"}
    assert "李明" in v1_persons
    assert "周杰" not in v1_persons

    v2 = _publish_v2(client, admin_headers)
    resp = client.post(
        f"/admin/workspaces/{ws['id']}/activate-rule", json={"version": v2}, headers=admin_headers
    )
    assert resp.status_code == 202
    drain()
    _wait(client, h)

    # v2 实体：周杰出现；历史 v1 证据仍可按版本读出且未被改写
    v2_ents = client.get(
        f"/api/documents/{up['doc_id']}/entities?rule_pack_version={v2}", headers=h
    ).get_json()
    v2_persons = {e["canonical"] for e in v2_ents["entities"] if e["entity_type"] == "person"}
    assert "周杰" in v2_persons
    v2_tech = {e["canonical"] for e in v2_ents["entities"] if e["entity_type"] == "tech"}
    assert "Rust" in v2_tech

    v1_ents_after = client.get(
        f"/api/documents/{up['doc_id']}/entities?rule_pack_version={v1}", headers=h
    ).get_json()
    v1_persons_after = {e["canonical"] for e in v1_ents_after["entities"] if e["entity_type"] == "person"}
    assert v1_persons == v1_persons_after, "回滚/升级不得改写历史抽取证据"

    rule = client.get(f"/admin/workspaces/{ws['id']}/rule", headers=admin_headers).get_json()
    assert rule["active_rule_pack_version"] == v2
    gen_id = rule["active_index_generation_id"]
    assert gen_id and rule["recent_generations"][0]["status"] == "active"
    assert rule["recent_generations"][0]["rule_pack_version"] == v2


def test_queries_see_one_generation_only(ws, client, drain, admin_headers):
    h = ws["headers"]
    _upload(client, h, "苹果 香蕉 苹果", "a.txt")
    drain()
    s_before = client.get("/api/search?q=苹果", headers=h).get_json()
    assert s_before["generation_id"]
    gen_before = s_before["generation_id"]

    v2 = _publish_v2(client, admin_headers)
    client.post(
        f"/admin/workspaces/{ws['id']}/activate-rule", json={"version": v2}, headers=admin_headers
    )
    # 重建期间持续查询：结果始终来自某一完整代，绝不报错或混读
    seen_gens = set()
    for i in range(5):
        r = client.get("/api/search?q=苹果", headers=h).get_json()
        seen_gens.add(r["generation_id"])
    assert gen_before in seen_gens  # 切换前一直看旧代
    drain()
    _wait(client, h)
    s_after = client.get("/api/search?q=苹果", headers=h).get_json()
    assert s_after["generation_id"] != gen_before
    assert s_after["result_count"] == 1
    # 旧代物理数据已删（无残留 posting）
    import sqlite3

    from conftest import _DB_PATH

    conn = sqlite3.connect(_DB_PATH)
    leftover = conn.execute(
        "SELECT COUNT(*) FROM postings WHERE generation_id=?", (gen_before,)
    ).fetchone()[0]
    conn.close()
    assert leftover == 0


def test_rebuild_does_not_miss_new_documents(ws, client, drain, admin_headers):
    """重建进行中上传的新文档，最终必须在新代里可搜。"""
    h = ws["headers"]
    _upload(client, h, "旧文档 内容 甲", "old.txt")
    drain()

    v2 = _publish_v2(client, admin_headers)
    client.post(
        f"/admin/workspaces/{ws['id']}/activate-rule", json={"version": v2}, headers=admin_headers
    )
    # 不 drain：模拟重建期间立刻到达的新文档
    up_new = _upload(client, h, "新文档 内容 乙 丙", "new.txt")

    drain()
    _wait(client, h)

    r = client.get("/api/search?q=新文档", headers=h).get_json()
    ids = [x["doc_id"] for x in r["results"]]
    assert up_new["doc_id"] in ids
    r2 = client.get("/api/search?q=甲", headers=h).get_json()
    assert r2["result_count"] == 1


def test_rollback_to_old_version_preserves_history(ws, client, drain, admin_headers):
    h = ws["headers"]
    v1 = ws["default_rule"]
    up = _upload(client, h, "周工用 Rust 写程序，李明说可以。", "d.txt")
    drain()

    v2 = _publish_v2(client, admin_headers)
    client.post(
        f"/admin/workspaces/{ws['id']}/activate-rule", json={"version": v2}, headers=admin_headers
    )
    drain()
    _wait(client, h)

    # 回滚到 v1（此时 v1 的旧代已被删除 → 走「重新构建」路径）
    resp = client.post(
        f"/admin/workspaces/{ws['id']}/rollback-rule", json={"version": v1}, headers=admin_headers
    )
    assert resp.status_code == 202
    body = resp.get_json()
    assert body["mode"] == "rebuild"
    drain()
    _wait(client, h)

    rule = client.get(f"/admin/workspaces/{ws['id']}/rule", headers=admin_headers).get_json()
    assert rule["active_rule_pack_version"] == v1
    # v2 的历史证据仍在（按版本可查），未被回滚改写
    v2_ents = client.get(
        f"/api/documents/{up['doc_id']}/entities?rule_pack_version={v2}", headers=h
    ).get_json()
    assert any(e["canonical"] == "Rust" for e in v2_ents["entities"])
    # 激活/回滚都有追加历史
    actions = [a["action"] for a in rule["history"]]
    assert actions == ["activate", "rollback"]


def test_concurrent_rebuild_rejected(ws, client, drain, admin_headers):
    h = ws["headers"]
    _upload(client, h, "文档 一", "a.txt")
    drain()
    v2 = _publish_v2(client, admin_headers)
    r1 = client.post(
        f"/admin/workspaces/{ws['id']}/activate-rule", json={"version": v2}, headers=admin_headers
    )
    assert r1.status_code == 202
    # 第二个不同版本立刻激活应被拒（有重建在跑）
    r2 = client.post(
        f"/admin/workspaces/{ws['id']}/activate-rule", json={"version": ws["default_rule"]},
        headers=admin_headers,
    )
    assert r2.status_code == 409
    drain()
