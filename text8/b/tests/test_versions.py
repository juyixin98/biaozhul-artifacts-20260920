import pytest

pytestmark = pytest.mark.db


def _simple_definition(key="ver_tpl", approvers=None):
    approvers = approvers or ["alice"]
    return {
        "key": key,
        "name": "版本测试",
        "nodes": [
            {"id": "start", "type": "start", "next": "a"},
            {
                "id": "a",
                "type": "approval",
                "name": "A",
                "mode": "all",
                "approvers": approvers,
                "on_reject": "rend",
                "next": "eend",
            },
            {"id": "eend", "type": "end", "outcome": "approved"},
            {"id": "rend", "type": "end", "outcome": "rejected"},
        ],
    }


def test_publish_validation_rejects_bad_definition(client):
    bad = _simple_definition()
    bad["nodes"][0]["next"] = "ghost"
    r = client.post("/templates", json={"definition": bad, "publish": True})
    assert r.status_code == 422
    codes = {i["code"] for i in r.json()["error"]["issues"]}
    assert "missing_reference" in codes


def test_draft_can_be_created_invalid_then_published_after_fix(client):
    # draft 不做语义校验的前提是结构能被 Pydantic 解析；这里演示 draft→publish
    d = _simple_definition()
    r = client.post("/templates", json={"definition": d, "publish": False})
    assert r.status_code == 201
    key = d["key"]
    assert r.json()["template"]["current_version_number"] is None

    r = client.post(f"/templates/{key}/versions/1/publish", json={})
    assert r.status_code == 200 and r.json()["status"] == "published"

    # 重复发布冲突
    r = client.post(f"/templates/{key}/versions/1/publish", json={})
    assert r.status_code == 409


def test_version_isolation_running_instance_uses_old_version(client):
    key = "iso_tpl"
    d1 = _simple_definition(key, approvers=["alice"])
    r = client.post("/templates", json={"definition": d1, "publish": True})
    assert r.status_code == 201

    # 发起 v1 实例
    r = client.post(
        "/instances",
        json={"template_key": key, "title": "旧实例", "context": {}},
        headers={"X-User": "tom"},
    )
    assert r.status_code == 201
    old = r.json()
    assert old["version_number"] == 1
    assert {t["assignee"] for t in old["pending_tasks"]} == {"alice"}

    # 发布 v2：审批人改为 bob
    d2 = _simple_definition(key, approvers=["bob"])
    r = client.post(f"/templates/{key}/versions", json={"definition": d2, "publish": True})
    assert r.status_code == 201 and r.json()["version"] == 2

    # 新实例绑定 v2
    r = client.post(
        "/instances",
        json={"template_key": key, "title": "新实例", "context": {}},
        headers={"X-User": "tom"},
    )
    new = r.json()
    assert new["version_number"] == 2
    assert {t["assignee"] for t in new["pending_tasks"]} == {"bob"}

    # 旧实例仍按 v1 执行：alice 可批，bob 不能动旧实例
    r = client.post(
        f"/instances/{old['id']}/decide?node_id=a",
        json={"request_id": "old-1", "expected_version": 1, "action": "approve"},
        headers={"X-User": "alice"},
    )
    assert r.status_code == 200 and r.json()["status"] == "approved"

    r = client.post(
        f"/instances/{old['id']}/decide?node_id=a",
        json={"request_id": "old-x", "expected_version": 1, "action": "approve"},
        headers={"X-User": "bob"},
    )
    assert r.status_code == 409

    # 指定版本发起：v1 仍然可用
    r = client.post(
        "/instances",
        json={"template_key": key, "version": 1, "title": "显式 v1", "context": {}},
        headers={"X-User": "tom"},
    )
    assert r.status_code == 201
    assert {t["assignee"] for t in r.json()["pending_tasks"]} == {"alice"}


def test_rollback_only_affects_new_instances(client):
    key = "rb_tpl"
    client.post("/templates", json={"definition": _simple_definition(key, ["alice"]), "publish": True})
    client.post(
        f"/templates/{key}/versions",
        json={"definition": _simple_definition(key, ["bob"]), "publish": True},
    )

    # 当前是 v2
    r = client.get(f"/templates/{key}")
    assert r.json()["current_version_number"] == 2

    # 回滚到 v1
    r = client.post(f"/templates/{key}/rollback/1")
    assert r.status_code == 200 and r.json()["version"] == 1
    r = client.get(f"/templates/{key}")
    assert r.json()["current_version_number"] == 1

    # 不指定版本的新实例拿到 v1
    r = client.post(
        "/instances",
        json={"template_key": key, "title": "回滚后的新单", "context": {}},
        headers={"X-User": "tom"},
    )
    assert r.json()["version_number"] == 1
    assert {t["assignee"] for t in r.json()["pending_tasks"]} == {"alice"}

    # 回滚到不存在的版本 -> 404
    r = client.post(f"/templates/{key}/rollback/99")
    assert r.status_code == 404


def test_published_version_is_immutable(client):
    """接口层面没有任何修改已发布版本定义的入口；只能新建版本。"""
    key = "imm_tpl"
    r = client.post(
        "/templates", json={"definition": _simple_definition(key), "publish": True}
    )
    tv = r.json()["version"]
    # 再次以相同定义发布 -> v2，而不是覆盖 v1
    r2 = client.post(
        f"/templates/{key}/versions",
        json={"definition": _simple_definition(key), "publish": True},
    )
    assert r2.json()["version"] == 2
    versions = client.get(f"/templates/{key}/versions").json()
    assert {v["version"] for v in versions} == {1, 2}
    assert all(v["status"] == "published" for v in versions)
    # v1 内容接口可读且不变
    v1 = client.get(f"/templates/{key}/versions/1").json()
    assert v1["id"] == tv["id"]


def test_expected_version_conflict_blocks_stale_client(client):
    key = "ver_conflict"
    client.post("/templates", json={"definition": _simple_definition(key), "publish": True})
    r = client.post(
        "/instances",
        json={"template_key": key, "title": "x", "context": {}},
        headers={"X-User": "tom"},
    )
    iid = r.json()["id"]
    # 发起一个 v2（客户端如果还带 v1 会被拒）
    client.post(
        f"/templates/{key}/versions",
        json={"definition": _simple_definition(key, ["zoe"]), "publish": True},
    )
    r = client.post(
        f"/instances/{iid}/decide?node_id=a",
        json={"request_id": "stale-1", "expected_version": 2, "action": "approve"},
        headers={"X-User": "alice"},
    )
    assert r.status_code == 409 and r.json()["error"]["code"] == "version_conflict"
