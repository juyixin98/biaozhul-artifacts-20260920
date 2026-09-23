"""密码学操作测试：SHA-256 快照摘要、HMAC-SHA256 签名、验签与篡改检测。"""

import hashlib
import hmac
import json

from app.clmm.crypto import (
    canonical_json,
    get_hmac_key,
    sha256_hex,
    sign_quote,
    snapshot_hash,
    verify_quote,
)


def test_canonical_json_deterministic():
    a = {"b": 1, "a": [1, 2, {"x": "你"}]}
    b = {"a": [1, 2, {"x": "你"}], "b": 1}
    assert canonical_json(a) == canonical_json(b)
    assert canonical_json(a) == '{"a":[1,2,{"x":"你"}],"b":1}'.encode("utf-8")


def test_sha256_matches_known_vector():
    # 空串的 SHA-256 标准测试向量
    assert sha256_hex(b"") == hashlib.sha256(b"").hexdigest()
    assert sha256_hex(b"abc") == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )


def test_snapshot_hash_changes_with_any_field():
    snap = {"pool_id": "p", "fee_ppm": 3000, "sqrt_price_x96": "79228162514264337593543950336"}
    h0 = snapshot_hash(snap)
    h1 = snapshot_hash({**snap, "fee_ppm": 3001})
    h2 = snapshot_hash({**snap, "sqrt_price_x96": "79228162514264337593543950337"})
    assert len({h0, h1, h2}) == 3
    assert all(len(h) == 64 for h in (h0, h1, h2))


def test_hmac_sign_and_verify_roundtrip():
    payload = {"quote_id": "q1", "result": {"amount_out_total": "997"}}
    sig = sign_quote(payload)
    assert sig == hmac.new(get_hmac_key(), canonical_json(payload), hashlib.sha256).hexdigest()
    assert verify_quote(payload, sig) is True


def test_signature_rejects_tampering():
    payload = {"quote_id": "q1", "result": {"amount_out_total": "997"}}
    sig = sign_quote(payload)
    tampered = json.loads(json.dumps(payload))
    tampered["result"]["amount_out_total"] = "998"
    assert verify_quote(tampered, sig) is False
    assert verify_quote(payload, sig[:-1] + ("0" if sig[-1] != "0" else "1")) is False
    assert verify_quote(payload, "not-a-signature") is False
    assert verify_quote(payload, 123) is False  # type: ignore[arg-type]
