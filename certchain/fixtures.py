"""本地测试用 PKI 生成。

所有密钥均为本地临时生成的 RSA 密钥，仅用于测试与演示，
不对应任何真实主体，不接入任何生产账号或生产 PKI。
证书签发使用 cryptography 库的标准 X.509 构建器。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

DAY = timedelta(days=1)
# 固定的演示用时间锚点，保证测试可重复
T0 = datetime(2020, 1, 1, tzinfo=timezone.utc)


def new_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def name(cn: str, org: str = "Offline Verify Lab") -> x509.Name:
    return x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, org),
            x509.NameAttribute(NameOID.COMMON_NAME, cn),
        ]
    )


def pem(cert: x509.Certificate) -> str:
    return cert.public_bytes(serialization.Encoding.PEM).decode("ascii")


def make_ca(
    cn: str,
    *,
    key: Optional[rsa.RSAPrivateKey] = None,
    issuer_cert: Optional[x509.Certificate] = None,
    issuer_key: Optional[rsa.RSAPrivateKey] = None,
    path_length: Optional[int] = None,
    not_before: datetime = T0,
    not_after: datetime = T0 + 3650 * DAY,
    serial: Optional[int] = None,
) -> tuple[x509.Certificate, rsa.RSAPrivateKey]:
    """生成 CA 证书；issuer_cert 为 None 时自签（作为信任根）。"""
    key = key or new_key()
    subject = name(cn)
    issuer = issuer_cert.subject if issuer_cert is not None else subject
    signing_key = issuer_key if issuer_key is not None else key
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(serial or x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .add_extension(x509.BasicConstraints(ca=True, path_length=path_length), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
    )
    if issuer_cert is not None:
        builder = builder.add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_cert.public_key()),
            critical=False,
        )
    cert = builder.sign(signing_key, hashes.SHA256())
    return cert, key


def make_leaf(
    cn: str,
    *,
    issuer_cert: x509.Certificate,
    issuer_key: rsa.RSAPrivateKey,
    san_dns: Optional[list[str]] = None,
    eku: Optional[list[x509.ObjectIdentifier]] = None,
    not_before: datetime = T0,
    not_after: datetime = T0 + 3650 * DAY,
    ca: bool = False,
) -> tuple[x509.Certificate, rsa.RSAPrivateKey]:
    """生成终端实体证书（ca=True 时用于构造“非标准”场景的对照）。"""
    key = new_key()
    san_dns = san_dns or [cn]
    eku = eku if eku is not None else [ExtendedKeyUsageOID.SERVER_AUTH]
    builder = (
        x509.CertificateBuilder()
        .subject_name(name(cn))
        .issuer_name(issuer_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=ca,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.ExtendedKeyUsage(eku), critical=False)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(d) for d in san_dns]), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_cert.public_key()),
            critical=False,
        )
    )
    cert = builder.sign(issuer_key, hashes.SHA256())
    return cert, key


def make_non_ca_issuer(
    cn: str,
    *,
    issuer_cert: x509.Certificate,
    issuer_key: rsa.RSAPrivateKey,
) -> tuple[x509.Certificate, rsa.RSAPrivateKey]:
    """生成一个“没有 CA 基本约束”的中间证书（BasicConstraints ca=False）。

    用于验收用例：它签发的叶子证书在链验证时必须被拒绝。
    """
    key = new_key()
    builder = (
        x509.CertificateBuilder()
        .subject_name(name(cn))
        .issuer_name(issuer_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(T0)
        .not_valid_after(T0 + 3650 * DAY)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,  # 明确不具备签发资格
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_cert.public_key()),
            critical=False,
        )
    )
    cert = builder.sign(issuer_key, hashes.SHA256())
    return cert, key


# ---------------------------------------------------------------------------
# 验收用例链：每个函数返回 dict，键为 PEM 字符串或 PEM 列表
# ---------------------------------------------------------------------------

def case_good() -> dict:
    """正常链：root(pathlen=1) -> intermediate -> leaf(SAN 匹配, serverAuth)。"""
    root, root_key = make_ca("Lab Root CA", path_length=1)
    inter, inter_key = make_ca(
        "Lab Intermediate CA", issuer_cert=root, issuer_key=root_key, path_length=0
    )
    leaf, _ = make_leaf(
        "service.example.local",
        issuer_cert=inter,
        issuer_key=inter_key,
        san_dns=["service.example.local"],
        eku=[ExtendedKeyUsageOID.SERVER_AUTH],
    )
    return {
        "leaf": pem(leaf),
        "intermediates": [pem(inter)],
        "trust_roots": [pem(root)],
        "hostname": "service.example.local",
    }


def case_expired() -> dict:
    """过期链：叶子证书 not_after = 2021-01-01，验证时刻默认 2026 年。"""
    root, root_key = make_ca("Lab Root CA (expired case)", path_length=1)
    inter, inter_key = make_ca(
        "Lab Intermediate CA (expired case)", issuer_cert=root, issuer_key=root_key, path_length=0
    )
    leaf, _ = make_leaf(
        "expired.example.local",
        issuer_cert=inter,
        issuer_key=inter_key,
        san_dns=["expired.example.local"],
        not_before=T0,
        not_after=datetime(2021, 1, 1, tzinfo=timezone.utc),
    )
    return {
        "leaf": pem(leaf),
        "intermediates": [pem(inter)],
        "trust_roots": [pem(root)],
        "hostname": "expired.example.local",
    }


def case_pathlen_violation() -> dict:
    """路径长度违规：root(pathlen=0) 下挂两级中间 CA，再签叶子。"""
    root, root_key = make_ca("Lab Root CA (pathlen=0)", path_length=0)
    inter1, inter1_key = make_ca(
        "Lab Intermediate L1", issuer_cert=root, issuer_key=root_key, path_length=1
    )
    inter2, inter2_key = make_ca(
        "Lab Intermediate L2", issuer_cert=inter1, issuer_key=inter1_key, path_length=0
    )
    leaf, _ = make_leaf(
        "deep.example.local",
        issuer_cert=inter2,
        issuer_key=inter2_key,
        san_dns=["deep.example.local"],
    )
    return {
        "leaf": pem(leaf),
        "intermediates": [pem(inter1), pem(inter2)],
        "trust_roots": [pem(root)],
        "hostname": "deep.example.local",
    }


def case_non_ca_intermediate() -> dict:
    """非 CA 中间证书：中间证书 BasicConstraints ca=False，仍签发了叶子。"""
    root, root_key = make_ca("Lab Root CA (non-ca case)", path_length=2)
    fake_inter, fake_inter_key = make_non_ca_issuer(
        "Lab Not-A-CA Intermediate", issuer_cert=root, issuer_key=root_key
    )
    leaf, _ = make_leaf(
        "victim.example.local",
        issuer_cert=fake_inter,
        issuer_key=fake_inter_key,
        san_dns=["victim.example.local"],
    )
    return {
        "leaf": pem(leaf),
        "intermediates": [pem(fake_inter)],
        "trust_roots": [pem(root)],
        "hostname": "victim.example.local",
    }


def case_same_name() -> dict:
    """同名证书链：两个同名根、两个同名中间 CA，密钥不同。

    叶子由 intermediate#1（root#1 体系）签发。
    - 正向：trust_roots=[root1]，intermediates 同时给出 [inter2(错误同名), inter1(正确)]，
      路径构建必须按签名/密钥选中 inter1，验证通过。
    - 反向：只给 inter2（同名但密钥不符），验证必须失败。
    """
    root1, root1_key = make_ca("Duplicate Root CA", path_length=1)
    root2, root2_key = make_ca("Duplicate Root CA", path_length=1)
    inter1, inter1_key = make_ca(
        "Duplicate Intermediate CA", issuer_cert=root1, issuer_key=root1_key, path_length=0
    )
    inter2, _ = make_ca(
        "Duplicate Intermediate CA", issuer_cert=root2, issuer_key=root2_key, path_length=0
    )
    leaf, _ = make_leaf(
        "dup.example.local",
        issuer_cert=inter1,
        issuer_key=inter1_key,
        san_dns=["dup.example.local"],
    )
    return {
        "leaf": pem(leaf),
        # 正确中间证书故意放在同名错误证书之后
        "intermediates": [pem(inter2), pem(inter1)],
        "intermediates_wrong_only": [pem(inter2)],
        "trust_roots": [pem(root1)],
        "trust_roots_other": [pem(root2)],
        "hostname": "dup.example.local",
    }


ALL_CASES = {
    "good": case_good,
    "expired": case_expired,
    "pathlen_violation": case_pathlen_violation,
    "non_ca_intermediate": case_non_ca_intermediate,
    "same_name": case_same_name,
}

# 演示/测试统一使用的显式验证时刻（落在 good 链有效期内、expired 叶子过期之后）
DEFAULT_VALIDATION_TIME = "2026-06-01T00:00:00Z"
