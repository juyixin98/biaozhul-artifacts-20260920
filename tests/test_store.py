"""KeyStore / ObjectStore 与主密钥轮换的服务级测试。"""

import pytest

from app import crypto
from app.store import KeyStore, ObjectStore, RotationInterrupted


@pytest.fixture()
def store():
    keys = KeyStore()
    return ObjectStore(keys)


def test_initial_key_is_v1(store):
    assert store._keys.current_id == "v1"
    assert [k.key_id for k in store._keys.list_keys()] == ["v1"]


def test_put_then_open(store):
    store.put("a", b"hello", block_size=2)
    assert store.open("a") == b"hello"
    assert store.metadata("a")["key_id"] == "v1"


def test_independent_dek_per_object(store):
    store.put("a", b"same")
    store.put("b", b"same")
    ha, _ = crypto.parse_header(store.get_container("a"))
    hb, _ = crypto.parse_header(store.get_container("b"))
    assert ha.wrapped_dek != hb.wrapped_dek


def test_rotation_rewraps_only_header_and_old_objects_decrypt(store):
    store.put("old1", b"old object one" * 3, block_size=5)
    store.put("old2", b"old object two", block_size=4)
    before = {n: store.get_container(n) for n in ("old1", "old2")}

    result = store.rotate_master()
    assert result["new_key_id"] == "v2"
    assert result["rewrapped"] == 2

    for name, old_blob in before.items():
        new_blob = store.get_container(name)
        _, old_body = crypto.parse_header(old_blob)
        _, new_body = crypto.parse_header(new_blob)
        assert old_blob[old_body:] == new_blob[new_body:]  # 密文块原样保留
        assert store.metadata(name)["key_id"] == "v2"
        assert store.open(name) == (b"old object one" * 3 if name == "old1" else b"old object two")
    # v1 旧密钥仍在, 旧容器副本仍可解密
    assert crypto.open_container(before["old1"], store._keys.resolve) == b"old object one" * 3


def test_rotation_interrupted_leaves_mixed_state_and_resume(store):
    """模拟轮换中断: 新主密钥已建立, 仅部分对象重包裹; 续跑后全部可解密。"""
    for i in range(4):
        store.put(f"obj{i}", f"内容{i}-".encode() * 10, block_size=3)
    originals = {f"obj{i}": store.open(f"obj{i}") for i in range(4)}

    with pytest.raises(RotationInterrupted) as exc:
        store.rotate_master(fail_after=2)
    assert exc.value.new_key_id == "v2"
    assert exc.value.rewrapped == 2
    assert exc.value.remaining == 2

    key_ids = {store.metadata(f"obj{i}")["key_id"] for i in range(4)}
    assert key_ids == {"v1", "v2"}  # 混合包裹状态
    # 中断状态下新旧对象都仍可解密 (v1、v2 密钥都在)
    for i in range(4):
        assert store.open(f"obj{i}") == originals[f"obj{i}"]

    result = store.rewrap_all()
    assert result["new_key_id"] == "v2"
    assert result["rewrapped"] == 2
    assert all(store.metadata(f"obj{i}")["key_id"] == "v2" for i in range(4))
    for i in range(4):
        assert store.open(f"obj{i}") == originals[f"obj{i}"]


def test_interrupted_resume_then_another_rotation(store):
    store.put("a", b"aaa")
    store.put("b", b"bbb")
    with pytest.raises(RotationInterrupted):
        store.rotate_master(fail_after=1)
    # 再次"全新轮换": 生成 v3, v1/v2 对象统一到 v3
    result = store.rotate_master()
    assert result["new_key_id"] == "v3"
    assert result["rewrapped"] == 2
    assert store.open("a") == b"aaa" and store.open("b") == b"bbb"


def test_discard_old_key_makes_old_objects_fail(store):
    store.put("a", b"secret")
    old_blob = store.get_container("a")
    store.rotate_master()  # a -> v2
    store._keys.discard("v1")
    # 新容器可解
    assert store.open("a") == b"secret"
    # 保留下来的旧容器因密钥删除无法解密
    with pytest.raises(crypto.KeyUnavailableError):
        crypto.open_container(old_blob, store._keys.resolve)


def test_cannot_discard_current_key(store):
    with pytest.raises(ValueError):
        store._keys.discard("v1")


def test_tampered_object_fails_at_service_level(store):
    store.put("a", b"x" * 60, block_size=10)
    blob = bytearray(store.get_container("a"))
    blob[-1] ^= 0x01
    store.set_container("a", bytes(blob))
    with pytest.raises(crypto.DecryptError):
        store.open("a")


def test_truncated_object_fails_at_service_level(store):
    store.put("a", b"y" * 60, block_size=10)
    store.set_container("a", store.get_container("a")[:-5])
    with pytest.raises(crypto.EnvelopeError):
        store.open("a")


def test_new_objects_after_rotation_use_new_key(store):
    store.put("old", b"old")
    store.rotate_master()
    store.put("new", b"new")
    assert store.metadata("old")["key_id"] == "v2"
    assert store.metadata("new")["key_id"] == "v2"
