"""密码学原语单元测试。"""

from __future__ import annotations

import pytest

from app import crypto


def test_sign_and_verify_roundtrip():
    kp = crypto.generate_keypair()
    content = b"artifact-bytes"
    digest = crypto.sha256_hex(content)
    nonce = crypto.generate_nonce()
    sig = crypto.sign_artifact(
        private_key_hex=kp.private_key_hex,
        digest_hex=digest,
        artifact_type="firmware",
        version="1.2.3",
        nonce_hex=nonce,
    )
    assert crypto.verify_artifact_signature(
        public_key_hex=kp.public_key_hex,
        signature_hex=sig,
        digest_hex=digest,
        artifact_type="firmware",
        version="1.2.3",
        nonce_hex=nonce,
    )


def test_tampered_digest_fails():
    kp = crypto.generate_keypair()
    nonce = crypto.generate_nonce()
    sig = crypto.sign_artifact(
        private_key_hex=kp.private_key_hex,
        digest_hex=crypto.sha256_hex(b"original"),
        artifact_type="firmware",
        version="1.0.0",
        nonce_hex=nonce,
    )
    # 正文被篡改 -> 摘要不同 -> 验签失败
    assert not crypto.verify_artifact_signature(
        public_key_hex=kp.public_key_hex,
        signature_hex=sig,
        digest_hex=crypto.sha256_hex(b"tampered"),
        artifact_type="firmware",
        version="1.0.0",
        nonce_hex=nonce,
    )


def test_cross_type_signature_rejected():
    kp = crypto.generate_keypair()
    digest = crypto.sha256_hex(b"x")
    nonce = crypto.generate_nonce()
    sig = crypto.sign_artifact(
        private_key_hex=kp.private_key_hex,
        digest_hex=digest,
        artifact_type="firmware",
        version="1.0.0",
        nonce_hex=nonce,
    )
    # 同一签名搬到另一类型上必须失败（域分离）
    assert not crypto.verify_artifact_signature(
        public_key_hex=kp.public_key_hex,
        signature_hex=sig,
        digest_hex=digest,
        artifact_type="container-image",
        version="1.0.0",
        nonce_hex=nonce,
    )


def test_cross_version_and_cross_nonce_rejected():
    kp = crypto.generate_keypair()
    digest = crypto.sha256_hex(b"x")
    nonce = crypto.generate_nonce()
    sig = crypto.sign_artifact(
        private_key_hex=kp.private_key_hex,
        digest_hex=digest,
        artifact_type="t",
        version="2.0.0",
        nonce_hex=nonce,
    )
    assert not crypto.verify_artifact_signature(
        public_key_hex=kp.public_key_hex,
        signature_hex=sig,
        digest_hex=digest,
        artifact_type="t",
        version="1.9.9",
        nonce_hex=nonce,
    )
    assert not crypto.verify_artifact_signature(
        public_key_hex=kp.public_key_hex,
        signature_hex=sig,
        digest_hex=digest,
        artifact_type="t",
        version="2.0.0",
        nonce_hex=crypto.generate_nonce(),
    )


def test_wrong_key_fails():
    a = crypto.generate_keypair()
    b = crypto.generate_keypair()
    nonce = crypto.generate_nonce()
    sig = crypto.sign_artifact(
        private_key_hex=a.private_key_hex,
        digest_hex=crypto.sha256_hex(b"x"),
        artifact_type="t",
        version="1.0.0",
        nonce_hex=nonce,
    )
    assert not crypto.verify_artifact_signature(
        public_key_hex=b.public_key_hex,
        signature_hex=sig,
        digest_hex=crypto.sha256_hex(b"x"),
        artifact_type="t",
        version="1.0.0",
        nonce_hex=nonce,
    )


def test_key_id_deterministic():
    kp = crypto.generate_keypair()
    assert kp.kid == crypto.key_id(kp.public_key_hex)
    assert len(kp.kid) == 64


def test_version_parsing_and_rollback():
    assert crypto.parse_version("1.2.3") == (1, 2, 3)
    for bad in ["1.2", "v1.2.3", "1.2.3-rc1", "01.2.3", "abc", ""]:
        with pytest.raises(crypto.CryptoError):
            crypto.parse_version(bad)
    assert crypto.is_rollback("1.0.0", "2.0.0")
    assert crypto.is_rollback("2.0.0", "2.0.0")  # 相等也算拒绝
    assert not crypto.is_rollback("2.0.1", "2.0.0")
    assert not crypto.is_rollback("3.0.0", None)


def test_root_rotation_domain_separated_from_artifact():
    """根轮换签名不能拿去当制品签名（前缀不同），反之亦然。"""

    kp = crypto.generate_keypair()
    digest = crypto.sha256_hex(b"x")
    nonce = crypto.generate_nonce()
    artifact_sig = crypto.sign_artifact(
        private_key_hex=kp.private_key_hex,
        digest_hex=digest,
        artifact_type="t",
        version="1.0.0",
        nonce_hex=nonce,
    )
    rotation_sig = crypto.sign_root_rotation(
        private_key_hex=kp.private_key_hex,
        new_root_version=2,
        new_threshold=1,
        new_threshold_keys=[kp.public_key_hex],
        new_signer_keys=[kp.public_key_hex],
    )
    assert artifact_sig != rotation_sig
    assert not crypto.verify_root_rotation_approval(
        public_key_hex=kp.public_key_hex,
        signature_hex=artifact_sig,
        new_root_version=2,
        new_threshold=1,
        new_threshold_keys=[kp.public_key_hex],
        new_signer_keys=[kp.public_key_hex],
    )


def test_rotation_approval_is_bound_to_new_root_content():
    kp = crypto.generate_keypair()
    new_pub = crypto.generate_keypair().public_key_hex
    sig = crypto.sign_root_rotation(
        private_key_hex=kp.private_key_hex,
        new_root_version=2,
        new_threshold=1,
        new_threshold_keys=[kp.public_key_hex],
        new_signer_keys=[new_pub],
    )
    # 新根内容里任何一个字段被改，批准签名失效
    assert not crypto.verify_root_rotation_approval(
        public_key_hex=kp.public_key_hex,
        signature_hex=sig,
        new_root_version=3,
        new_threshold=1,
        new_threshold_keys=[kp.public_key_hex],
        new_signer_keys=[new_pub],
    )
