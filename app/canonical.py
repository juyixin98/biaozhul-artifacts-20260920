"""确定性的签名报文编码。

签名绑定的所有字段都经过「域分隔前缀 + 长度前缀」编码，避免字段拼接歧义，
并保证两端（签名方/验证方）对同一逻辑报文得到完全一致的字节串。
"""
from __future__ import annotations

# 域分隔：三类报文即使字段巧合相同也不能互相复用签名
ARTIFACT_DOMAIN = b"ARTIFACT-SIGNATURE/v1\n"
ROOT_DOMAIN = b"TRUST-ROOT-DESCRIPTOR/v1\n"
ROOT_APPROVAL_DOMAIN = b"TRUST-ROOT-ROTATION-APPROVAL/v1\n"


def _field(value: bytes) -> bytes:
    """8 字节大端长度前缀 + 原始字节。"""
    return len(value).to_bytes(8, "big") + value


def _join(domain: bytes, fields: list[bytes]) -> bytes:
    return domain + b"".join(_field(f) for f in fields)


def artifact_message(
    digest: bytes, artifact_type: str, version: str, nonce: bytes
) -> bytes:
    """制品签名报文：绑定 内容摘要 + 制品类型 + 版本 + 签名者一次性 nonce。"""
    return _join(
        ARTIFACT_DOMAIN,
        [
            digest,
            artifact_type.encode("utf-8"),
            version.encode("utf-8"),
            nonce,
        ],
    )


def root_message(
    version: int,
    root_threshold: int,
    root_signers_raw: list[bytes],
    artifact_threshold: int,
    artifact_signers_raw: list[bytes],
) -> bytes:
    """信任根描述符报文：轮换批准签名即针对该字节串而签。

    签名者公钥列表先按原始字节排序后编码，消除顺序歧义。
    """
    root_blob = b"".join(_field(k) for k in sorted(root_signers_raw))
    artifact_blob = b"".join(_field(k) for k in sorted(artifact_signers_raw))
    return _join(
        ROOT_DOMAIN,
        [
            str(version).encode("ascii"),
            str(root_threshold).encode("ascii"),
            root_blob,
            str(artifact_threshold).encode("ascii"),
            artifact_blob,
        ],
    )


def root_approval_message(descriptor: bytes, nonce: bytes) -> bytes:
    """旧根密钥对新根的批准签名报文：绑定新根描述符 + 一次性 nonce。"""
    return _join(ROOT_APPROVAL_DOMAIN, [descriptor, nonce])
