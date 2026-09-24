"""HTTP 接口端到端测试 (FastAPI TestClient, 不监听端口)。

覆盖验收场景: 正常存取、轮换后旧对象可解密、轮换中断/续跑、
错误密钥、头部篡改、密文截断 —— 失败时接口返回 4xx 且不带明文。
"""

import base64

import pytest
from fastapi.testclient import TestClient

from app import crypto
from app.main import create_app
from app.store import KeyStore, ObjectStore


@pytest.fixture()
def ctx():
    keys = KeyStore()
    objs = ObjectStore(keys)
    app = create_app(keys=keys, objects=objs)
    return TestClient(app), keys, objs


@pytest.fixture()
def client(ctx):
    return ctx[0]


# ---------- 正常路径 ----------

def test_put_raw_bytes_and_get(client):
    data = b"hello \x00\x01 binary"
    r = client.put("/objects/doc", content=data, headers={"content-type": "application/octet-stream"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["name"] == "doc"
    assert body["metadata"]["key_id"] == "v1"
    assert body["metadata"]["plaintext_len"] == len(data)

    r = client.get("/objects/doc")
    assert r.status_code == 200
    assert r.content == data
    assert r.headers["x-plaintext-length"] == str(len(data))


def test_put_json_base64_and_get(client):
    data = "中文信封加密".encode()
    r = client.put("/objects/j", json={"plaintext_b64": base64.b64encode(data).decode()})
    assert r.status_code == 201, r.text
    assert client.get("/objects/j").content == data


def test_put_with_block_size_multi_block(client):
    r = client.put("/objects/big", content=b"x" * 100, params={"block_size": 10})
    assert r.status_code == 201
    assert r.json()["metadata"]["block_size"] == 10
    assert client.get("/objects/big").content == b"x" * 100


def test_put_empty_object(client):
    r = client.put("/objects/empty", content=b"", headers={"content-type": "application/octet-stream"})
    assert r.status_code == 201
    assert client.get("/objects/empty").content == b""


def test_list_and_metadata(client):
    client.put("/objects/a", content=b"aa")
    client.put("/objects/b", content=b"bb")
    assert client.get("/objects").json()["objects"] == ["a", "b"]
    meta = client.get("/objects/a/metadata").json()
    assert {"version", "key_id", "block_size", "plaintext_len", "salt_hex"} <= set(meta)


def test_get_missing_returns_404(client):
    assert client.get("/objects/nope").status_code == 404
    assert client.get("/objects/nope/metadata").status_code == 404


def test_invalid_base64_returns_400(client):
    r = client.put("/objects/bad", json={"plaintext_b64": "not_base64!!!"})
    assert r.status_code == 400
    assert r.json()["error"] == "format_error"


def test_invalid_block_size_returns_422(client):
    r = client.put("/objects/bad", content=b"x", params={"block_size": 0})
    assert r.status_code == 422


# ---------- 主密钥轮换 ----------

def test_keys_listing_and_rotation(client):
    info = client.get("/keys").json()["keys"]
    assert [k["key_id"] for k in info] == ["v1"]
    assert info[0]["is_current"] is True

    client.put("/objects/a", content=b"alpha" * 10, params={"block_size": 3})
    r = client.post("/keys/rotate")
    assert r.status_code == 200
    assert r.json() == {"new_key_id": "v2", "rewrapped": 1, "total_pending": 1}

    assert client.get("/objects/a/metadata").json()["key_id"] == "v2"
    assert client.get("/objects/a").content == b"alpha" * 10

    keys = client.get("/keys").json()["keys"]
    assert [k["key_id"] for k in keys] == ["v1", "v2"]
    assert keys[1]["is_current"] is True


def test_rotation_interrupt_then_resume_via_api(client, ctx):
    _app, _keys, objs = ctx
    for i in range(4):
        client.put(f"/objects/o{i}", content=f"p{i}".encode() * 5, params={"block_size": 2})

    r = client.post("/keys/rotate", params={"fail_after": 2})
    assert r.status_code == 500
    detail = r.json()
    assert detail["error"] == "rotation_interrupted"
    assert detail["new_key_id"] == "v2"
    assert detail["rewrapped"] == 2
    assert detail["remaining"] == 2

    key_ids = {client.get(f"/objects/o{i}/metadata").json()["key_id"] for i in range(4)}
    assert key_ids == {"v1", "v2"}
    # 中断态下全部对象仍可解密 (新旧密钥都在), 无部分明文问题
    for i in range(4):
        assert client.get(f"/objects/o{i}").content == f"p{i}".encode() * 5

    r = client.post("/keys/rewrap")
    assert r.status_code == 200
    assert r.json() == {"new_key_id": "v2", "rewrapped": 2, "total_pending": 2}
    for i in range(4):
        assert client.get(f"/objects/o{i}/metadata").json()["key_id"] == "v2"
        assert client.get(f"/objects/o{i}").content == f"p{i}".encode() * 5


def test_old_objects_still_decryptable_after_rotation(client):
    client.put("/objects/a", content=b"first")
    client.put("/objects/b", content=b"second")
    client.post("/keys/rotate")
    client.put("/objects/c", content=b"third")  # 轮换后新建, 直接用 v2
    for name, pt in [("a", b"first"), ("b", b"second"), ("c", b"third")]:
        assert client.get(f"/objects/{name}").content == pt
    client.post("/keys/rotate")  # 再轮换一轮: v2/v1 对象统一到 v3
    for name, pt in [("a", b"first"), ("b", b"second"), ("c", b"third")]:
        assert client.get(f"/objects/{name}").content == pt
        assert client.get(f"/objects/{name}/metadata").json()["key_id"] == "v3"


# ---------- 故障 / 攻击场景 ----------

def test_wrong_key_returns_409_no_plaintext(ctx):
    client, keys, objs = ctx
    # 两个 v1 对象, 轮换时只重包裹 1 个 -> legacy 仍由 v1 包裹
    client.put("/objects/legacy", content=b"legacy secret data")
    client.put("/objects/current", content=b"current data")
    client.post("/keys/rotate", params={"fail_after": 1})
    assert objs.metadata("legacy")["key_id"] == "v1"
    assert objs.metadata("current")["key_id"] == "v2"
    # 删除旧主密钥 v1 (模拟旧密钥被安全销毁 / 错误 KMS 环境)
    r = client.delete("/keys/v1")
    assert r.status_code == 200
    # v1 对象无法解密: 409 且响应中不包含明文
    r = client.get("/objects/legacy")
    assert r.status_code == 409
    assert r.json()["error"] == "key_unavailable"
    assert "legacy" not in r.text
    # v2 对象不受影响
    assert client.get("/objects/current").content == b"current data"


def test_header_tamper_returns_400(ctx):
    client, keys, objs = ctx
    client.put("/objects/a", content=b"x" * 40, params={"block_size": 10})
    blob = bytearray(objs.get_container("a"))
    # key_id 在偏移 6 起 ("v1"), 翻转末位且保持可解析 (v1 仍存在, 仅 AAD 失配的构造见单元测试)
    # 这里翻转 wrapped DEK 内一字节: GCM tag 失配
    # 定位 wrapped_dek: 4+1+1+2+8+4+8+2 = 30 为 wrapped 起点
    blob[30 + 12] ^= 0xFF
    objs.set_container("a", bytes(blob))
    r = client.get("/objects/a")
    assert r.status_code == 400
    assert r.json()["error"] == "decrypt_failed"


def test_ciphertext_truncation_returns_400(ctx):
    client, keys, objs = ctx
    client.put("/objects/a", content=b"y" * 40, params={"block_size": 10})
    objs.set_container("a", objs.get_container("a")[:-3])  # 砍掉末块 tag 3 字节
    r = client.get("/objects/a")
    assert r.status_code == 400
    assert r.json()["error"] in {"decrypt_failed", "format_error"}


def test_ciphertext_bitflip_returns_400(ctx):
    client, keys, objs = ctx
    client.put("/objects/a", content=b"z" * 40, params={"block_size": 10})
    blob = bytearray(objs.get_container("a"))
    blob[-1] ^= 0x01
    objs.set_container("a", bytes(blob))
    r = client.get("/objects/a")
    assert r.status_code == 400
    assert "z" * 40 not in r.text  # 不泄露明文


def test_cannot_delete_current_key(client):
    assert client.delete("/keys/v1").status_code == 400


def test_tampered_object_after_rotation_fails(ctx):
    client, keys, objs = ctx
    client.put("/objects/a", content=b"payload" * 10, params={"block_size": 5})
    client.post("/keys/rotate")
    blob = bytearray(objs.get_container("a"))
    blob[-1] ^= 0x01
    objs.set_container("a", bytes(blob))
    r = client.get("/objects/a")
    assert r.status_code == 400
    # 重包裹幂等操作也拒绝损坏对象 (DEK 解包前数据块虽不验证,
    # 但本处损坏的是末块; rewrap 只解 DEK 仍可成功 —— 明文始终拿不到)
