"""审计日志测试：哈希链完整性 + 无密钥/明文泄露。"""

import json

from keyvault import KeyService


def test_audit_records_all_operations(active_svc):
    env = active_svc.encrypt(b"data")
    active_svc.decrypt(env)
    active_svc.generate_key()
    active_svc.activate("v2")
    active_svc.deactivate("v2")
    active_svc.destroy("v2")
    events = [rec["event"] for rec in active_svc.audit]
    for expected in (
        "KEY_GENERATED",
        "KEY_ACTIVATED",
        "KEY_DEACTIVATED",
        "KEY_DESTROYED",
        "ENCRYPT",
        "DECRYPT",
    ):
        assert expected in events, f"审计缺少事件 {expected}"


def test_audit_chain_verifies(active_svc):
    for i in range(10):
        active_svc.encrypt(f"msg-{i}".encode())
    ok, reason = active_svc.audit.verify()
    assert ok, reason


def test_audit_tamper_detected(active_svc, data_dir):
    active_svc.encrypt(b"important")
    log_path = data_dir / "audit.log"
    lines = log_path.read_text(encoding="utf-8").splitlines()
    # 篡改中间一条记录的内容
    rec = json.loads(lines[2])
    rec["details"]["version"] = "v999"
    lines[2] = json.dumps(rec, ensure_ascii=False)
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    fresh = KeyService(data_dir)  # 重新加载后校验
    ok, reason = fresh.audit.verify()
    assert not ok
    assert "哈希校验失败" in reason or "prev_hash" in reason


def test_audit_truncation_detected(active_svc, data_dir):
    for i in range(5):
        active_svc.encrypt(f"m{i}".encode())
    log_path = data_dir / "audit.log"
    lines = log_path.read_text(encoding="utf-8").splitlines()
    # 删除中间一行（攻击者试图抹掉某次操作）
    del lines[3]
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    fresh = KeyService(data_dir)
    ok, reason = fresh.audit.verify()
    assert not ok


def test_audit_contains_no_key_material(active_svc, data_dir):
    """审计日志中绝不出现密钥材料（十六进制 / base64 任何形式）。"""
    active_svc.encrypt(b"top secret plaintext")
    active_svc.generate_key()
    active_svc.activate("v2")
    active_svc.encrypt(b"more secrets")
    log_text = (data_dir / "audit.log").read_text(encoding="utf-8")

    import base64

    for vid in ("v1", "v2"):
        key = active_svc.store.load_key_material(vid)
        assert key.hex() not in log_text
        assert base64.b64encode(key).decode() not in log_text


def test_audit_contains_no_plaintext(active_svc, data_dir):
    secret = "绝密明文-do-not-log"
    active_svc.encrypt(secret.encode())
    log_text = (data_dir / "audit.log").read_text(encoding="utf-8")
    assert secret not in log_text
    import base64

    assert base64.b64encode(secret.encode()).decode() not in log_text


def test_audit_records_denied_attempts(active_svc):
    """被拒绝的操作也必须留痕（安全审计的关键）。"""
    env = active_svc.encrypt(b"x")
    active_svc.deactivate("v1")
    active_svc.destroy("v1")
    try:
        active_svc.decrypt(env)
    except Exception:
        pass
    denied = [r for r in active_svc.audit if r["event"] == "DECRYPT_DENIED"]
    assert denied and denied[-1]["details"]["reason"] == "destroyed"
