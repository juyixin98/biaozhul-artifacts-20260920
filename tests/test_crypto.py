"""密码学操作真实执行测试：密钥生成、签名、验签、防篡改、确定性。"""

from app.crypto import Signer, canonical_bytes, sha256_hex, verify_signature


def test_sign_verify_roundtrip(tmp_path):
    signer = Signer.load_or_create(tmp_path / "k.pem")
    payload = {"a": 1, "b": [1, 2, 3], "c": {"x": "y"}}
    digest, sig, pub = signer.sign_result(payload)
    assert len(digest) == 64  # SHA-256 hex
    assert verify_signature(pub, payload, sig) is True


def test_tamper_rejected(tmp_path):
    signer = Signer.load_or_create(tmp_path / "k.pem")
    _, sig, pub = signer.sign_result({"v": 1})
    assert verify_signature(pub, {"v": 2}, sig) is False


def test_wrong_key_rejected(tmp_path):
    s1 = Signer.load_or_create(tmp_path / "k1.pem")
    s2 = Signer.load_or_create(tmp_path / "k2.pem")
    _, sig, _ = s1.sign_result({"v": 1})
    assert verify_signature(s2.public_key_b64, {"v": 1}, sig) is False


def test_key_persistence(tmp_path):
    path = tmp_path / "persist.pem"
    s1 = Signer.load_or_create(path)
    s2 = Signer.load_or_create(path)  # 第二次应加载同一把
    assert s1.public_key_b64 == s2.public_key_b64
    assert s1.key_id == s2.key_id


def test_key_id_is_pubkey_hash(tmp_path):
    import hashlib

    import base64

    signer = Signer.load_or_create(tmp_path / "k.pem")
    raw = base64.b64decode(signer.public_key_b64)
    assert signer.key_id == "ed25519-" + hashlib.sha256(raw).hexdigest()[:32]


def test_canonical_bytes_deterministic():
    a = canonical_bytes({"x": 1, "y": 2})
    b = canonical_bytes({"y": 2, "x": 1})
    assert a == b
    assert sha256_hex({"x": 1}) == sha256_hex({"x": 1})


def test_ed25519_signature_is_real(tmp_path):
    # 真实 64 字节 Ed25519 签名（base64 后 88 字符）
    import base64

    signer = Signer.load_or_create(tmp_path / "k.pem")
    _, sig, _ = signer.sign_result({"ok": True})
    assert len(base64.b64decode(sig)) == 64
