"""端到端 HTTP 接口测试（FastAPI TestClient）。"""

import io

import pytest
from fastapi.testclient import TestClient

from app.builder import bump_all
from app.config import Settings
from app.main import create_app


@pytest.fixture
def env(tmp_path, repo):
    settings = Settings(
        data_dir=str(tmp_path / "data"),
        bootstrap_root_public=repo.root_pub_hex(),
        admin_token="test-admin-token",
    )
    app = create_app(settings)
    return TestClient(app), repo


def _bundle_files(bundle):
    files = []
    for name, data in bundle.files.items():
        files.append(("files", (name, io.BytesIO(data), "application/octet-stream")))
    return files


def test_health_initial(env):
    client, _ = env
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["trusted"]["root"] is None


def test_bootstrap_via_http_and_download(env):
    client, repo = env
    with client:
        with io.BytesIO(repo.bundle().root) as root_f, \
                io.BytesIO(repo.latest["timestamp"]) as ts_f, \
                io.BytesIO(repo.latest["snapshot"]) as snap_f, \
                io.BytesIO(repo.latest["targets"]) as tgt_f:
            r = client.post(
                "/updates",
                files=[
                    ("root", ("root.json", root_f, "application/json")),
                    ("timestamp", ("timestamp.json", ts_f, "application/json")),
                    ("snapshot", ("snapshot.json", snap_f, "application/json")),
                    ("targets", ("targets.json", tgt_f, "application/json")),
                    ("files", ("app.bin", io.BytesIO(b"app-version-1"),
                               "application/octet-stream")),
                ],
            )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["root_version"] == 1

    # 下载已验证的目标
    r = client.get("/targets/app.bin")
    assert r.status_code == 200
    assert r.content == b"app-version-1"
    assert r.headers["x-content-sha256"]

    # 元数据读取
    r = client.get("/metadata/root")
    assert r.status_code == 200
    assert r.json()["signed"]["_type"] == "root"

    r = client.get("/health")
    assert r.json()["trusted"]["root"] == {"version": 1}
    assert r.json()["installed_targets"] == ["app.bin"]


def test_update_without_root_after_bootstrap(env):
    client, repo = env
    _bootstrap(client, repo)

    bump_all(repo, {"app.bin": b"app-version-2"})
    r = client.post("/updates", files=_role_files(repo, include_root=False) + [
        ("files", ("app.bin", io.BytesIO(b"app-version-2"), "application/octet-stream")),
    ])
    assert r.status_code == 200, r.text
    assert r.json()["targets_version"] == 2


def test_attack_over_http_returns_error_and_no_state_change(env):
    client, repo = env
    _bootstrap(client, repo)
    health_before = client.get("/health").json()

    evil = b"X" * len(repo.files["app.bin"])  # 等长，直接命中哈希校验
    attack = repo.tamper_file("app.bin", evil)
    r = client.post("/updates", files=[
        ("timestamp", ("t.json", io.BytesIO(attack.timestamp), "application/json")),
        ("snapshot", ("s.json", io.BytesIO(attack.snapshot), "application/json")),
        ("targets", ("g.json", io.BytesIO(attack.targets), "application/json")),
        ("files", ("app.bin", io.BytesIO(evil), "application/octet-stream")),
    ])
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "TARGET_HASH_MISMATCH"
    assert client.get("/health").json() == health_before


def test_frozen_timestamp_rollback_over_http(env):
    client, repo = env
    _bootstrap(client, repo)
    v1 = repo.bundle()
    bump_all(repo, {"app.bin": b"app-version-2"})
    _publish(client, repo, b"app-version-2")

    frozen = repo.freeze_old_timestamp(v1)
    r = client.post("/updates", files=[
        ("timestamp", ("t.json", io.BytesIO(frozen.timestamp), "application/json")),
        ("snapshot", ("s.json", io.BytesIO(repo.latest["snapshot"]), "application/json")),
        ("targets", ("g.json", io.BytesIO(repo.latest["targets"]), "application/json")),
        ("files", ("app.bin", io.BytesIO(b"app-version-2"), "application/octet-stream")),
    ])
    assert r.status_code in (409, 422)
    assert r.json()["error"]["code"] in (
        "TIMESTAMP_ROLLBACK", "HASH_MISMATCH")

    # 合法 v3 恢复
    bump_all(repo, {"app.bin": b"app-version-3"})
    r = _publish(client, repo, b"app-version-3")
    assert r.status_code == 200
    assert r.json()["timestamp_version"] == 3
    assert client.get("/targets/app.bin").content == b"app-version-3"


def test_root_rotation_over_http(env):
    client, repo = env
    _bootstrap(client, repo)

    from app.builder import RoleKeys
    from app import crypto_utils
    new_roles = {r: RoleKeys([crypto_utils.generate_keypair()[0]])
                 for r in ("root", "targets", "snapshot", "timestamp")}
    repo.rotate_root(new_roles)
    r = client.post("/updates", files=_role_files(repo) + [
        ("files", ("app.bin", io.BytesIO(repo.files["app.bin"]),
                   "application/octet-stream")),
    ])
    assert r.status_code == 200, r.text
    assert r.json()["root_version"] == 2


def test_missing_role_field_returns_422(env):
    client, _ = env
    r = client.post("/updates", files=[
        ("timestamp", ("t.json", io.BytesIO(b"{}"), "application/json")),
    ])
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "MISSING_ROLE"


def test_unsupported_media_type(env):
    client, _ = env
    r = client.post("/updates", json={"x": 1})
    assert r.status_code == 415


def test_admin_reset_requires_token(env):
    client, repo = env
    _bootstrap(client, repo)
    assert client.post("/admin/reset").status_code == 401
    r = client.post("/admin/reset", headers={"Authorization": "Bearer test-admin-token"})
    assert r.status_code == 200
    assert client.get("/health").json()["trusted"]["root"] is None


# ---- helpers --------------------------------------------------------------

def _role_files(repo, *, include_root=True):
    out = []
    if include_root:
        out.append(("root", ("root.json", io.BytesIO(repo.latest["root"]),
                             "application/json")))
    out += [
        ("timestamp", ("timestamp.json", io.BytesIO(repo.latest["timestamp"]),
                       "application/json")),
        ("snapshot", ("snapshot.json", io.BytesIO(repo.latest["snapshot"]),
                      "application/json")),
        ("targets", ("targets.json", io.BytesIO(repo.latest["targets"]),
                     "application/json")),
    ]
    return out


def _bootstrap(client, repo):
    r = client.post("/updates", files=_role_files(repo) + [
        ("files", ("app.bin", io.BytesIO(repo.files["app.bin"]),
                   "application/octet-stream")),
    ])
    assert r.status_code == 200, r.text
    return r


def _publish(client, repo, content):
    return client.post("/updates", files=_role_files(repo, include_root=False) + [
        ("files", ("app.bin", io.BytesIO(content), "application/octet-stream")),
    ])
