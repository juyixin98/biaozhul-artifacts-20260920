"""持久化、审计日志、哈希链防篡改与“审计无密钥泄露”测试。"""

from __future__ import annotations

import base64
import json

import pytest

from keyversion.errors import AuditLogTampered
from keyversion.service import KeyService
from keyversion.store import KeyStore


def test_state_reload_restores_versions_and_pointer(tmp_path):
    d = tmp_path / "ks"
    s1 = KeyStore(d).open()
    svc = KeyService(s1)
    svc.rotate()
    svc.rotate()
    svc.rotate()
    s1.close()

    s2 = KeyStore(d).open()
    svc2 = KeyService(s2)
    assert len(svc2.list_versions()) == 3
    assert svc2.active_version() is not None
    states = {v.version_id: v.state for v in svc2.list_versions()}
    assert list(states.values()) == ["retired", "retired", "active"]
    s2.close()


def test_destroyed_version_persists_without_material(tmp_path):
    d = tmp_path / "ks"
    s1 = KeyStore(d).open()
    svc = KeyService(s1)
    svc.rotate()
    v1 = svc.active_version()
    svc.destroy(v1.version_id)
    s1.close()

    state = json.loads((d / "state.json").read_text())
    rec = state["versions"][v1.version_id]
    assert rec["wrapped_dek"] is None and rec["state"] == "destroyed"

    s2 = KeyStore(d).open()
    assert s2.version_state(v1.version_id) == "destroyed"
    s2.close()


def test_audit_entries_cover_lifecycle(initialized_service):
    service = initialized_service
    v1 = service.active_version()
    service.rotate()
    service.deactivate(service.active_version().version_id)
    service.destroy(v1.version_id)
    actions = [(e.action, e.result) for e in service.list_audit()]
    assert ("activate", "success") in actions
    assert ("deactivate", "success") in actions
    assert ("destroy", "success") in actions
    seqs = [e.seq for e in service.list_audit()]
    assert seqs == list(range(1, len(seqs) + 1))


def test_audit_log_never_contains_secrets(initialized_service):
    """验收：审计日志（落盘字节）中绝不出现明文、DEK、KEK 或被包装密钥。"""
    service = initialized_service
    secret_plaintext = b"PLAINTTEXT-SECRET-MARKER-12345"
    ct, _ = service.encrypt(secret_plaintext)
    service.rotate()
    service.decrypt(ct)
    store = service.store  # 日志条目在追加时已 fsync，可直接读字节

    raw_audit = (store._audit_path).read_bytes()
    raw_state = (store._state_path).read_bytes()
    raw_kek = (store._kek_path).read_bytes()

    assert b"PLAINTTEXT-SECRET-MARKER-12345" not in raw_audit
    assert b"PLAINTTEXT-SECRET-MARKER-12345" not in raw_state
    # 原始 KEK 字节不出现在审计中
    assert raw_kek not in raw_audit
    assert raw_kek not in raw_state
    # state.json 中的 wrapped_dek 不等于明文 DEK（已被 KEK 包装）
    state = json.loads(raw_state)
    for rec in state["versions"].values():
        if rec["wrapped_dek"]:
            blob = base64.b64decode(rec["wrapped_dek"])
            assert blob != raw_kek and len(blob) > 32
            assert secret_plaintext not in blob
    # 审计条目只记录长度，不记录密文本体
    for line in raw_audit.splitlines():
        entry = json.loads(line)
        assert "plaintext" not in entry["detail"]
        assert "ciphertext" not in entry["detail"] or isinstance(
            entry["detail"].get("ciphertext_len"), int
        )


def test_audit_hash_chain_links_entries(initialized_service):
    service = initialized_service
    service.rotate()
    entries = service.list_audit()
    for i, e in enumerate(entries):
        assert e.prev_hash == ("" if i == 0 else entries[i - 1].entry_hash)


def test_audit_tampering_detected(initialized_service):
    service = initialized_service
    service.rotate()
    path = service.store._audit_path
    lines = path.read_bytes().splitlines(keepends=True)
    # 解析中间条目，改写 detail 后重新序列化（哈希必然对不上）
    idx = len(lines) // 2
    entry = json.loads(lines[idx])
    entry["detail"] = {"tampered": True}
    lines[idx] = json.dumps(entry, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    path.write_bytes(b"".join(lines))
    with pytest.raises(AuditLogTampered):
        KeyStore(service.store.data_dir).open()


def test_audit_truncated_last_line_tolerated_and_truncated_middle_rejected(tmp_path):
    d = tmp_path / "ks"
    s1 = KeyStore(d).open()
    svc = KeyService(s1)
    svc.rotate()
    svc.rotate()
    path = d / "audit.log"
    lines = path.read_bytes().splitlines(keepends=True)
    # 模拟崩溃：在最后一条完整记录后追加半条新记录（无换行、不可解析）
    path.write_bytes(b"".join(lines) + b'{"seq": 99, "action": "gen')
    s2 = KeyStore(d).open()  # 不应抛异常，半行被截掉
    entries = KeyService(s2).list_audit()
    assert len(entries) == len(lines)
    assert entries[-1].seq == len(lines)
    s2.close()

    # 中间删掉整行 -> 链断裂
    good = path.read_bytes().splitlines(keepends=True)
    path.write_bytes(b"".join(good[:1] + good[2:]))
    with pytest.raises(AuditLogTampered):
        KeyStore(d).open()


def test_state_file_permissions_restrictive(tmp_path):
    d = tmp_path / "ks"
    s = KeyStore(d).open()
    KeyService(s).rotate()
    assert oct((d / "state.json").stat().st_mode & 0o777) == "0o600"
    assert oct((d / "kek.key").stat().st_mode & 0o777) == "0o600"
    assert oct(d.stat().st_mode & 0o777) == "0o700"
    s.close()


def test_wrong_kek_rejected_on_load(tmp_path):
    d = tmp_path / "ks"
    s1 = KeyStore(d).open()
    KeyService(s1).rotate()
    s1.close()
    # 替换 KEK -> 指纹/解包校验失败，拒绝启动
    (d / "kek.key").write_bytes(b"X" * 32)
    with pytest.raises(Exception):
        KeyStore(d).open()
