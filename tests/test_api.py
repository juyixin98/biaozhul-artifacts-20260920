"""FastAPI 端到端测试：通过 HTTP 上传/下载，覆盖完整升级链与攻击拒绝。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import metadata as md
from app.api import create_app

from conftest import Lab

V1 = {md.TARGETS: 1, md.SNAPSHOT: 1, md.TIMESTAMP: 1}
V2 = {md.TARGETS: 2, md.SNAPSHOT: 2, md.TIMESTAMP: 2}
V3 = {md.TARGETS: 3, md.SNAPSHOT: 3, md.TIMESTAMP: 3}


@pytest.fixture
def client(tmp_path) -> TestClient:
    return TestClient(create_app(str(tmp_path / "data")))


@pytest.fixture
def started(client: TestClient, lab: Lab) -> TestClient:
    r = client.post(
        "/api/bootstrap",
        files={"root": ("1.root.json", lab.root_v1(), "application/json")},
    )
    assert r.status_code == 200, r.text
    return client


def _post_update(client: TestClient, rel: dict, files_payload: dict,
                 root: bytes | None = None):
    form_files = [
        ("timestamp", ("timestamp.json", rel[md.TIMESTAMP], "application/json")),
        ("snapshot", ("snapshot.json", rel[md.SNAPSHOT], "application/json")),
        ("targets", ("targets.json", rel[md.TARGETS], "application/json")),
    ]
    if root is not None:
        form_files.append(
            ("root", ("2.root.json", root, "application/json"))
        )
    for name, data in files_payload.items():
        form_files.append(
            ("files", (name, data, "application/octet-stream"))
        )
    return client.post("/api/update", files=form_files)


def test_health(client: TestClient) -> None:
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_full_upgrade_chain_and_download(
    started: TestClient, lab: Lab
) -> None:
    client = started

    r1 = lab.release(lab.keys_a, V1, {"app.txt": b"release-1"})
    resp = _post_update(client, r1, {"app.txt": b"release-1"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["versions"]["root"] == 1

    state = client.get("/api/state").json()
    assert state["bootstrapped"] is True
    assert state["targets"] == ["app.txt"]

    got = client.get("/api/targets/app.txt")
    assert got.status_code == 200
    assert got.content == b"release-1"
    assert len(got.headers["X-Content-SHA256"]) == 64

    r2 = lab.release(lab.keys_a, V2, {"app.txt": b"release-2"})
    resp = _post_update(client, r2, {"app.txt": b"release-2"})
    assert resp.status_code == 200
    assert got.content != resp  # 仅防误用，真正断言见下载

    got2 = client.get("/api/targets/app.txt")
    assert got2.content == b"release-2"


def test_update_before_bootstrap_409(client: TestClient, lab: Lab) -> None:
    rel = lab.release(lab.keys_a, V1, {"a": b"1"})
    resp = _post_update(client, rel, {"a": b"1"})
    assert resp.status_code == 409
    assert resp.json()["error"] == "no_state"


def test_double_bootstrap_409(started: TestClient, lab: Lab) -> None:
    resp = started.post(
        "/api/bootstrap",
        files={"root": ("1.root.json", lab.root_v1(), "application/json")},
    )
    assert resp.status_code == 409


def test_rollback_via_http_returns_400(
    started: TestClient, lab: Lab
) -> None:
    client = started
    r1 = lab.release(lab.keys_a, V1, {"app.txt": b"v1"})
    assert _post_update(client, r1, {"app.txt": b"v1"}).status_code == 200
    r2 = lab.release(lab.keys_a, V2, {"app.txt": b"v2"})
    assert _post_update(client, r2, {"app.txt": b"v2"}).status_code == 200

    resp = _post_update(client, r1, {"app.txt": b"v1"})
    assert resp.status_code == 400
    assert resp.json()["error"] == "rollback"
    # 被拒后状态不变
    assert client.get("/api/state").json()["versions"]["timestamp"] == 2


def test_payload_swap_via_http_returns_400(
    started: TestClient, lab: Lab
) -> None:
    rel = lab.release(lab.keys_a, V1, {"app.txt": b"legit"})
    resp = _post_update(started, rel, {"app.txt": b"EVIL-BYTES"})
    assert resp.status_code == 400
    assert resp.json()["error"] == "hash"
    assert started.get("/api/targets/app.txt").status_code == 404


def test_root_rotation_then_continue(
    started: TestClient, lab: Lab
) -> None:
    client = started
    # 先接受 A 时代的 v1
    r1a = lab.release(lab.keys_a, V1, {"app.txt": b"a1"})
    assert _post_update(client, r1a, {"app.txt": b"a1"}).status_code == 200

    # v2 同时完成 root v2 轮换（A+B 双签）与 B 签发的元数据
    root2 = lab.root_v2_rotation()
    r2b = lab.release(lab.keys_b, V2, {"app.txt": b"b2"})
    resp = _post_update(client, r2b, {"app.txt": b"b2"}, root=root2)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["versions"]["root"] == 2
    assert body["versions"]["timestamp"] == 2

    # A 的旧密钥再签名 -> 拒绝
    r4a = lab.release(lab.keys_a, {
        md.TARGETS: 4, md.SNAPSHOT: 4, md.TIMESTAMP: 4
    }, {"app.txt": b"a4"})
    resp = _post_update(client, r4a, {"app.txt": b"a4"})
    assert resp.status_code == 400
    assert resp.json()["error"] == "signature"

    # B 继续合法升级 -> 恢复成功
    r4b = lab.release(lab.keys_b, {
        md.TARGETS: 4, md.SNAPSHOT: 4, md.TIMESTAMP: 4
    }, {"app.txt": b"b4"})
    assert _post_update(client, r4b, {"app.txt": b"b4"}).status_code == 200
    assert client.get("/api/targets/app.txt").content == b"b4"


def test_reset_clears_state(started: TestClient) -> None:
    assert started.post("/api/reset").status_code == 200
    assert started.get("/api/state").json()["bootstrapped"] is False


def test_metadata_endpoint(started: TestClient, lab: Lab) -> None:
    client = started
    rel = lab.release(lab.keys_a, V1, {"app.txt": b"x"})
    _post_update(client, rel, {"app.txt": b"x"})
    ts = client.get("/api/metadata/timestamp")
    assert ts.status_code == 200
    assert ts.json()["signed"]["version"] == 1
    assert client.get("/api/metadata/bogus").status_code == 400
