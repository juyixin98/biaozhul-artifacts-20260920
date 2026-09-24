"""密码学操作测试：SHA-256 与 HMAC-SHA256 都是真实计算，可用外部工具复算。"""

import hashlib
import hmac
import json

import pytest

from groundseg import crypto


KNOWN_BYTES = b"abc"
KNOWN_SHA256 = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


def test_request_sha256_matches_known_vector():
    # 与 NIST 已知摘要 / hashlib 独立复算一致
    assert crypto.request_sha256(KNOWN_BYTES) == KNOWN_SHA256
    assert crypto.request_sha256(b"") == hashlib.sha256(b"").hexdigest()


def test_request_sha256_rejects_non_bytes():
    with pytest.raises(TypeError):
        crypto.request_sha256("not bytes")  # type: ignore[arg-type]


def test_canonical_json_is_order_independent():
    a = {"b": 1, "a": [1, 2, 3], "c": {"z": 1, "y": 2}}
    b = {"c": {"y": 2, "z": 1}, "a": [1, 2, 3], "b": 1}
    assert crypto.canonical_json(a) == crypto.canonical_json(b)


def test_response_sha256_real_hash():
    obj = {"x": 1, "y": "地面"}
    expected = hashlib.sha256(crypto.canonical_json(obj)).hexdigest()
    assert crypto.response_sha256(obj) == expected


def test_hmac_sign_and_verify_roundtrip(monkeypatch):
    monkeypatch.setenv("GROUNDSEG_HMAC_KEY", "test-secret-key")
    obj = {"a": 1, "b": [1, 2, 3]}
    sig = crypto.sign_response(obj)
    assert sig is not None
    assert sig["algorithm"] == "HMAC-SHA256"
    # 独立用标准库复算 MAC，而不是复用被测函数
    expected = hmac.new(
        b"test-secret-key", crypto.canonical_json(obj), hashlib.sha256
    ).hexdigest()
    assert sig["mac"] == expected
    assert crypto.verify_mac(obj, sig["mac"], b"test-secret-key")
    assert not crypto.verify_mac(obj, sig["mac"], b"wrong-key")
    # 篡改一个字节即验签失败
    tampered = {"a": 2, "b": [1, 2, 3]}
    assert not crypto.verify_mac(tampered, sig["mac"], b"test-secret-key")


def test_hmac_signature_excludes_signature_field(monkeypatch):
    monkeypatch.setenv("GROUNDSEG_HMAC_KEY", "k")
    body = {"x": 1, "signature": {"mac": "stale", "algorithm": "HMAC-SHA256"}}
    sig = crypto.sign_response(body)
    # 对不含 signature 的载荷重新签名应与返回值一致
    payload = {"x": 1}
    expected = hmac.new(b"k", crypto.canonical_json(payload),
                        hashlib.sha256).hexdigest()
    assert sig["mac"] == expected


def test_no_hmac_key_means_no_signature(monkeypatch):
    monkeypatch.delenv("GROUNDSEG_HMAC_KEY", raising=False)
    assert crypto.sign_response({"a": 1}) is None
    assert crypto.get_hmac_key() is None


def test_sha256_agrees_with_command_line(tmp_path):
    # 与外部 openssl/sha256sum 真实计算对照（至少有一个可用）
    import subprocess
    f = tmp_path / "data.bin"
    payload = bytes(range(256))
    f.write_bytes(payload)
    ours = crypto.request_sha256(payload)
    tool_result = None
    for cmd in (["sha256sum", str(f)], ["openssl", "dgst", "-sha256", str(f)]):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
            if "sha256sum" in cmd[0]:
                tool_result = out.split()[0]          # <hash>  <file>
            else:
                tool_result = out.strip().split()[-1]  # SHA2-256(file)= <hash>
            break
        except (FileNotFoundError, subprocess.CalledProcessError):
            continue
    assert tool_result is not None, "环境中需要 sha256sum 或 openssl"
    assert ours == tool_result
