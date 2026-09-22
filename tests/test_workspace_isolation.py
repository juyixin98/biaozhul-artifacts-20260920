"""工作区权限隔离：密钥不通、内容去重共享但读不到对方文档。"""
from __future__ import annotations

import io

from conftest import SAMPLE_TEXT_A, SAMPLE_TEXT_B


def _upload(client, headers, text, name="d.txt"):
    return client.post(
        "/api/documents",
        data={"file": (io.BytesIO(text.encode("utf-8")), name)},
        headers=headers,
        content_type="multipart/form-data",
    ).get_json()


def test_missing_or_wrong_key_rejected(client, ws):
    assert client.get("/api/documents").status_code == 401
    assert client.get("/api/documents", headers={"X-Workspace-Key": "wk_bogus"}).status_code == 401


def test_cross_workspace_access_denied(workspace_factory, client, drain):
    a = workspace_factory("alpha")
    b = workspace_factory("beta")
    up = _upload(client, a["headers"], SAMPLE_TEXT_A, "a-secret.txt")
    _upload(client, b["headers"], SAMPLE_TEXT_B, "b.txt")
    drain()

    # B 不能按 id 读 A 的文档（枚举攻击也防住）
    assert client.get(f"/api/documents/{up['doc_id']}", headers=b["headers"]).status_code == 404
    assert client.get(f"/api/documents/{up['doc_id']}/content", headers=b["headers"]).status_code == 404
    assert client.get(f"/api/documents/{up['doc_id']}/entities", headers=b["headers"]).status_code == 404
    assert client.get("/api/search?q=李明", headers=b["headers"]).get_json()["result_count"] == 0

    # B 的作业列表里看不到 A 的作业 id
    r = client.post(f"/api/jobs/{999999}/retry", headers=b["headers"])
    assert r.status_code == 404

    # B 的导出不含 A 的内容
    exported = client.get("/api/export?format=json", headers=b["headers"]).get_json()
    assert all(d["name"] != "a-secret.txt" for d in exported["documents"])


def test_same_content_shared_blob_but_isolated(workspace_factory, client, drain):
    a = workspace_factory("alpha2")
    b = workspace_factory("beta2")
    up_a = _upload(client, a["headers"], SAMPLE_TEXT_A, "same.txt")
    up_b = _upload(client, b["headers"], SAMPLE_TEXT_A, "same.txt")
    drain()
    assert up_a["sha256"] == up_b["sha256"]
    # 两个工作区各持独立文档行与独立抽取结果
    assert up_a["doc_id"] != up_b["doc_id"]
    ea = client.get(f"/api/documents/{up_a['doc_id']}/entities", headers=a["headers"]).status_code
    eb = client.get(f"/api/documents/{up_b['doc_id']}/entities", headers=b["headers"]).status_code
    assert ea == eb == 200
    # 交叉读被拒
    assert client.get(f"/api/documents/{up_a['doc_id']}/entities", headers=b["headers"]).status_code == 404


def test_workspace_dedup_within_workspace(ws, client, drain):
    up1 = _upload(client, ws["headers"], SAMPLE_TEXT_A, "x.txt")
    up2 = _upload(client, ws["headers"], SAMPLE_TEXT_A, "y.txt")
    drain()
    assert up1["doc_id"] == up2["doc_id"]
    assert up2["deduplicated"] is True


def test_admin_key_required(client, ws):
    r = client.post(
        f"/admin/workspaces/{ws['id']}/activate-rule", json={"version": "x"}
    )
    assert r.status_code == 401
    r2 = client.post(
        f"/admin/workspaces/{ws['id']}/activate-rule",
        json={"version": "x"},
        headers={"X-Admin-Key": "wrong"},
    )
    assert r2.status_code == 401
