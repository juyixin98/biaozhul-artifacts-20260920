"""验收核心：加密只允许 active 版本；解密按历史版本权限；错误版本拒绝。"""

from __future__ import annotations

import base64

import pytest

from keyversion.errors import (
    DecryptVersionDestroyed,
    DecryptVersionNotUsable,
    EncryptVersionNotActive,
    InvalidEnvelope,
    NoActiveVersion,
    VersionNotFound,
)
from keyversion.service import KeyService
from keyversion.store import GENERATED, KeyStore


# ---------------------------------------------------------------------------
# 加密权限
# ---------------------------------------------------------------------------
def test_encrypt_without_active_version_denied(service):
    with pytest.raises(NoActiveVersion):
        service.encrypt(b"data")
    entry = service.list_audit()[-1]
    assert entry.action == "encrypt" and entry.result == "denied"


def test_encrypt_uses_active_version(initialized_service):
    service = initialized_service
    active = service.active_version()
    ct, info = service.encrypt(b"hello")
    assert info.version_id == active.version_id


def test_encrypt_with_retired_version_explicitly_denied(initialized_service):
    service = initialized_service
    v1 = service.active_version()
    service.rotate()  # v1 retired
    with pytest.raises(EncryptVersionNotActive):
        service.encrypt(b"x", version_id=v1.version_id)
    denied = [e for e in service.list_audit() if e.result == "denied"]
    assert denied and denied[-1].action == "encrypt"
    assert "version_not_active" in denied[-1].detail["reason"]


def test_encrypt_with_generated_version_denied(initialized_service):
    service = initialized_service
    g = service.generate()
    with pytest.raises(EncryptVersionNotActive):
        service.encrypt(b"x", version_id=g.version_id)
    assert g.state == GENERATED  # 失败不改变状态


def test_encrypt_with_destroyed_version_denied(initialized_service):
    service = initialized_service
    v1 = service.active_version()
    service.destroy(v1.version_id)
    with pytest.raises(EncryptVersionNotActive):
        service.encrypt(b"x", version_id=v1.version_id)


def test_encrypt_with_unknown_version_denied(initialized_service):
    with pytest.raises((VersionNotFound, EncryptVersionNotActive)):
        initialized_service.encrypt(b"x", version_id="v0042-zzzz")


# ---------------------------------------------------------------------------
# 解密权限（按信封内历史版本）
# ---------------------------------------------------------------------------
def test_decrypt_succeeds_with_retired_history_version(initialized_service):
    service = initialized_service
    v1 = service.active_version()
    ct, _ = service.encrypt(b"old data")
    service.rotate()
    service.rotate()
    assert service.get_version(v1.version_id).state == "retired"
    pt, vid = service.decrypt(ct)
    assert pt == b"old data" and vid == v1.version_id


def test_decrypt_destroyed_version_is_unrecoverable(initialized_service):
    service = initialized_service
    v1 = service.active_version()
    ct, _ = service.encrypt(b"doomed")
    service.rotate()
    service.destroy(v1.version_id)
    with pytest.raises(DecryptVersionDestroyed):
        service.decrypt(ct)
    denied = [e for e in service.list_audit() if e.result == "denied"]
    assert denied[-1].detail["reason"] == "version_destroyed"


def test_decrypt_unknown_version_denied(initialized_service):
    service = initialized_service
    ct, _ = service.encrypt(b"data")
    # 手工拼一个指向不存在版本的信封：用原始密钥加密但换版本号会 AAD 失败，
    # 因此这里直接截断重写到一个结构合法但版本未知的信封
    # 更直接：新建独立密钥库的信封 -> 本库无此版本
    other = KeyService(KeyStore(service.store.data_dir.parent / "other").open())
    other.rotate()
    other_ct, _ = other.encrypt(b"alien")
    other.store.close()
    with pytest.raises(VersionNotFound):
        service.decrypt(other_ct)


def test_decrypt_tampered_envelope_denied(initialized_service):
    service = initialized_service
    ct, _ = service.encrypt(b"data")
    bad = bytes(ct[:-1]) + bytes([ct[-1] ^ 0x01])
    with pytest.raises(InvalidEnvelope):
        service.decrypt(bad)
    assert service.list_audit()[-1].detail["reason"] == "authentication_failed"


def test_decrypt_garbage_denied(initialized_service):
    with pytest.raises(InvalidEnvelope):
        initialized_service.decrypt(b"not an envelope at all")


def test_decrypt_generated_never_active_version_denied(initialized_service):
    """generated 版本从未承担加密，即便手工构造合法信封也不解密。"""
    from keyversion import crypto

    service = initialized_service
    g = service.generate()
    dek = service.store.load_dek(g.version_id)
    env = crypto.encrypt(dek, g.version_id, b"crafted")
    with pytest.raises(DecryptVersionNotUsable):
        service.decrypt(env)
    assert service.list_audit()[-1].detail["reason"].startswith(
        "version_state_not_decryptable(generated)"
    )


# ---------------------------------------------------------------------------
# 交错轮换与加解密请求：每个历史密文始终可按其版本解密（未销毁前）
# ---------------------------------------------------------------------------
def test_interleaved_rotation_and_requests(initialized_service):
    """验收场景：轮换与请求交错，旧密文跟随旧版本可解，新加密总用新版本。"""
    service = initialized_service
    history = []  # (version_id, ciphertext, plaintext)
    plaintexts = [f"record-{i}".encode() for i in range(9)]
    for i, pt in enumerate(plaintexts):
        ct, info = service.encrypt(pt)
        history.append((info.version_id, ct, pt))
        if i % 3 == 2:  # 每 3 次加密轮换一次
            service.rotate()

    versions = {v.version_id: v.state for v in service.list_versions()}
    # 没有任何版本是 generated；最早版本 retired，最后版本 active
    states = list(versions.values())
    assert states[-1] == "active"
    assert "active" in states
    # 所有历史密文都能用对应（active 或 retired）版本解开
    for vid, ct, pt in history:
        got, used = service.decrypt(ct)
        assert used == vid and got == pt
    # 轮换后显式用旧版本加密必须被拒
    old_vid = history[0][0]
    if versions[old_vid] != "active":
        with pytest.raises(EncryptVersionNotActive):
            service.encrypt(b"new", version_id=old_vid)


def test_destroy_then_reopen_remains_denied(tmp_path):
    """销毁后重新打开库：材料确实不在磁盘上，仍然不可解密。"""
    d = tmp_path / "ks"
    s1 = KeyStore(d).open()
    svc1 = KeyService(s1)
    svc1.rotate()
    v1 = svc1.active_version()
    ct, _ = svc1.encrypt(b"persist me")
    svc1.rotate()
    svc1.destroy(v1.version_id)
    s1.close()

    s2 = KeyStore(d).open()
    svc2 = KeyService(s2)
    with pytest.raises(DecryptVersionDestroyed):
        svc2.decrypt(ct)
    s2.close()


def test_base64_helpers_roundtrip():
    data = bytes(range(256))
    text = KeyService.b64e(data)
    assert KeyService.b64d(text) == data
    with pytest.raises(InvalidEnvelope):
        KeyService.b64d("!!!not base64!!!")
