"""离线 X.509 证书链验证核心逻辑。

使用 cryptography 的 x509.verification 模块（成熟验证库，纯本地验证）：
- 核对有效期（notBefore / notAfter）
- 核对扩展用途（EKU: serverAuth）
- 核对基本约束与路径长度（BasicConstraints CA / pathlen）
- 核对 DNS 名（SAN dNSName / CN 回退由库处理）
- 不访问任何在线吊销服务（无 CRL/OCSP 网络请求）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from cryptography import x509
from cryptography.x509.verification import (
    PolicyBuilder,
    Store,
    VerificationError,
)


class CertificateInputError(ValueError):
    """输入证书无法解析时抛出。"""


@dataclass
class VerificationResult:
    valid: bool
    chain_subjects: list[str] = field(default_factory=list)
    error: str | None = None


def _load_pem_certificates(pem_data: str, field_name: str) -> list[x509.Certificate]:
    """解析一段可能包含多张证书的 PEM 文本。"""
    try:
        certs = x509.load_pem_x509_certificates(pem_data.encode("utf-8"))
    except Exception as exc:  # cryptography 抛出 ValueError 等
        raise CertificateInputError(f"{field_name}: PEM 解析失败: {exc}") from exc
    if not certs:
        raise CertificateInputError(f"{field_name}: 未找到任何 PEM 证书")
    return certs


def verify_certificate_chain(
    leaf_pem: str,
    intermediates_pem: list[str],
    trust_roots_pem: list[str],
    expected_dns_name: str,
    validation_time: datetime | None = None,
) -> VerificationResult:
    """离线验证服务器证书链。

    参数:
        leaf_pem: 叶证书 PEM。
        intermediates_pem: 候选中间证书 PEM 列表（顺序不限，验证器自行构链）。
        trust_roots_pem: 显式信任根 PEM 列表（只信任这些根，不用系统根）。
        expected_dns_name: 期望的 DNS 名（按服务器证书策略核对 SAN）。
        validation_time: 验证时刻，默认当前 UTC 时间。
    """
    if not expected_dns_name or not expected_dns_name.strip():
        raise CertificateInputError("expected_dns_name 不能为空")

    leaf_certs = _load_pem_certificates(leaf_pem, "leaf_certificate_pem")
    if len(leaf_certs) != 1:
        raise CertificateInputError("leaf_certificate_pem 必须且只能包含一张叶证书")
    leaf = leaf_certs[0]

    intermediates: list[x509.Certificate] = []
    for i, pem in enumerate(intermediates_pem):
        intermediates.extend(_load_pem_certificates(pem, f"intermediate_certificates_pem[{i}]"))

    roots: list[x509.Certificate] = []
    for i, pem in enumerate(trust_roots_pem):
        roots.extend(_load_pem_certificates(pem, f"trust_roots_pem[{i}]"))
    if not roots:
        raise CertificateInputError("trust_roots_pem 至少需要一个信任根")

    if validation_time is None:
        validation_time = datetime.now(timezone.utc)
    elif validation_time.tzinfo is None:
        validation_time = validation_time.replace(tzinfo=timezone.utc)

    try:
        store = Store(roots)
        builder = PolicyBuilder().store(store).time(validation_time)
        verifier = builder.build_server_verifier(x509.DNSName(expected_dns_name))
        chain = verifier.verify(leaf, intermediates)
    except VerificationError as exc:
        return VerificationResult(valid=False, error=str(exc))

    return VerificationResult(
        valid=True,
        chain_subjects=[cert.subject.rfc4514_string() for cert in chain],
    )
