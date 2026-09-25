"""离线证书链验证核心逻辑。

设计约束：
- 只使用 cryptography 库提供的成熟 X.509 验证实现
  （cryptography.x509.verification，底层为 Rust 的 webpki 风格验证器），
  不自创任何密码算法或验证算法。
- 所有输入（信任根、中间证书、叶子证书、验证时刻、用途、主机名）
  必须由调用方显式提供，无任何隐式默认值。
- 不做任何网络访问：不抓 AIA 证书、不查 CRL/OCSP。
  因此吊销状态一律标记为“未验证”，由响应中的 revocation_checked / revocation_note 明示。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from cryptography import x509
from cryptography.x509.verification import (
    DNSName,
    PolicyBuilder,
    Store,
    VerificationError,
)

PURPOSES = ("server_tls", "client_tls")

REVOCATION_NOTE = (
    "离线验证：未执行 CRL/OCSP 吊销检查，链中所有证书的吊销状态未知。"
    "调用方如需吊销语义，必须自行离线提供吊销信息并另行核查。"
)


class VerifyInputError(ValueError):
    """请求输入不合法（对应 HTTP 400）。"""


def _parse_pem_cert(pem: str, what: str) -> x509.Certificate:
    try:
        return x509.load_pem_x509_certificate(pem.encode("utf-8"))
    except Exception as exc:  # cryptography 抛出 ValueError 及其子类
        raise VerifyInputError(f"{what} 不是合法的 PEM 编码 X.509 证书: {exc}") from exc


def _parse_time(value: str) -> datetime:
    """解析 ISO 8601 验证时刻；缺省时区按 UTC 处理（README 中明示）。"""
    try:
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        moment = datetime.fromisoformat(text)
    except Exception as exc:
        raise VerifyInputError(f"validation_time 不是合法 ISO 8601 时间: {value!r}") from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def _cert_summary(cert: x509.Certificate) -> dict[str, Any]:
    return {
        "subject": cert.subject.rfc4514_string(),
        "issuer": cert.issuer.rfc4514_string(),
        "serial_number": format(cert.serial_number, "x"),
        "not_before": cert.not_valid_before_utc.isoformat(),
        "not_after": cert.not_valid_after_utc.isoformat(),
        "is_ca": _is_ca(cert),
    }


def _is_ca(cert: x509.Certificate) -> bool:
    try:
        bc = cert.extensions.get_extension_for_class(x509.BasicConstraints).value
        return bool(bc.ca)
    except x509.ExtensionNotFound:
        return False


def verify_chain(
    *,
    leaf_pem: str,
    intermediates_pem: list[str],
    trust_roots_pem: list[str],
    validation_time_iso: Optional[str] = None,
    purpose: str,
    hostname: Optional[str] = None,
) -> dict[str, Any]:
    """验证一条证书链，返回结构化结果（不抛验证异常，验证失败体现在 valid=false）。

    参数全部为显式输入：
    - leaf_pem: 叶子证书 PEM
    - intermediates_pem: 未排序的中间证书 PEM 列表（可为空）
    - trust_roots_pem: 信任根 PEM 列表（至少一个）
    - validation_time_iso: 验证时刻，ISO 8601；缺省时区按 UTC
    - purpose: "server_tls"（必须给 hostname）或 "client_tls"
    - hostname: server_tls 用途下要校验的 DNS 主机名
    """
    if not validation_time_iso:
        raise VerifyInputError("必须显式提供 validation_time（ISO 8601）")
    if purpose not in PURPOSES:
        raise VerifyInputError(f"purpose 必须是 {PURPOSES} 之一，收到: {purpose!r}")
    if purpose == "server_tls" and not hostname:
        raise VerifyInputError("purpose=server_tls 时必须显式提供 hostname")
    if not trust_roots_pem:
        raise VerifyInputError("必须显式提供至少一个信任根 trust_roots")

    moment = _parse_time(validation_time_iso)
    leaf = _parse_pem_cert(leaf_pem, "leaf")
    intermediates = [_parse_pem_cert(p, f"intermediates[{i}]") for i, p in enumerate(intermediates_pem)]
    roots = [_parse_pem_cert(p, f"trust_roots[{i}]") for i, p in enumerate(trust_roots_pem)]

    store = Store(roots)
    builder = PolicyBuilder().store(store).time(moment)
    if purpose == "server_tls":
        verifier = builder.build_server_verifier(DNSName(hostname))
    else:
        verifier = builder.build_client_verifier()

    result: dict[str, Any] = {
        "valid": False,
        "chain": None,
        "error": None,
        "validation_time": moment.isoformat(),
        "purpose": purpose,
        "hostname": hostname if purpose == "server_tls" else None,
        # 吊销语义：本服务永不联网核查，显式声明
        "revocation_checked": False,
        "revocation_note": REVOCATION_NOTE,
    }
    try:
        chain = verifier.verify(leaf, intermediates)
    except VerificationError as exc:
        result["error"] = str(exc)
        return result
    result["valid"] = True
    result["chain"] = [_cert_summary(c) for c in chain]
    return result
