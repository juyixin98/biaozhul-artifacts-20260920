"""生成测试用本地 PKI：根 CA、中间 CA、各类正常/异常叶证书。

供 pytest 夹具与 scripts/generate_test_pki.py 共用。
全部使用 RSA-2048 + SHA-256，仅用于本地测试。
"""

from __future__ import annotations

import datetime

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

_ONE_DAY = datetime.timedelta(days=1)
_NOW = datetime.datetime.now(datetime.timezone.utc)


def _key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _name(cn: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def _base_builder(
    subject: x509.Name,
    issuer: x509.Name,
    public_key,
    not_before: datetime.datetime,
    not_after: datetime.datetime,
) -> x509.CertificateBuilder:
    return (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(public_key)
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
    )


def make_ca(
    cn: str,
    path_length: int | None,
    issuer_cert: x509.Certificate | None = None,
    issuer_key=None,
    not_before: datetime.datetime | None = None,
    not_after: datetime.datetime | None = None,
):
    """创建 CA 证书；issuer_cert 为 None 时自签（根 CA）。"""
    key = _key()
    subject = _name(cn)
    issuer = issuer_cert.subject if issuer_cert else subject
    sign_key = issuer_key if issuer_key else key
    nb = not_before or (_NOW - 730 * _ONE_DAY)
    na = not_after or (_NOW + 3650 * _ONE_DAY)
    issuer_public_key = issuer_cert.public_key() if issuer_cert else key.public_key()
    builder = (
        _base_builder(subject, issuer, key.public_key(), nb, na)
        .add_extension(
            x509.BasicConstraints(ca=True, path_length=path_length), critical=True
        )
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
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_public_key),
            critical=False,
        )
    )
    cert = builder.sign(sign_key, hashes.SHA256())
    return cert, key


def make_leaf(
    cn: str,
    issuer_cert: x509.Certificate,
    issuer_key,
    dns_names: list[str],
    not_before: datetime.datetime | None = None,
    not_after: datetime.datetime | None = None,
):
    """创建服务器叶证书（EKU=serverAuth，SAN=dns_names）。"""
    key = _key()
    nb = not_before or (_NOW - _ONE_DAY)
    na = not_after or (_NOW + 825 * _ONE_DAY)
    builder = (
        _base_builder(_name(cn), issuer_cert.subject, key.public_key(), nb, na)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(n) for n in dns_names]), critical=False)
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_cert.public_key()),
            critical=False,
        )
    )
    cert = builder.sign(issuer_key, hashes.SHA256())
    return cert, key


def make_self_signed(cn: str, dns_names: list[str]):
    """创建一张不在任何信任链中的自签“服务器”证书。"""
    key = _key()
    builder = (
        _base_builder(_name(cn), _name(cn), key.public_key(), _NOW - _ONE_DAY, _NOW + 365 * _ONE_DAY)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(n) for n in dns_names]), critical=False)
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(key.public_key()),
            critical=False,
        )
    )
    cert = builder.sign(key, hashes.SHA256())
    return cert, key


def pem(cert: x509.Certificate) -> str:
    return cert.public_bytes(serialization.Encoding.PEM).decode("ascii")


class TestPKI:
    """一套完整的测试 PKI，属性均为 PEM 字符串。"""

    def __init__(self) -> None:
        # 根 CA（pathlen=1）→ 中间 CA（pathlen=0）→ 叶证书
        self.root_cert, root_key = make_ca("Test Root CA", path_length=1)
        self.inter_cert, inter_key = make_ca(
            "Test Intermediate CA", path_length=0, issuer_cert=self.root_cert, issuer_key=root_key
        )

        # 正常叶证书
        self.leaf_cert, _ = make_leaf(
            "www.example.com", self.inter_cert, inter_key, ["www.example.com"]
        )
        # 过期叶证书
        self.expired_leaf_cert, _ = make_leaf(
            "www.example.com",
            self.inter_cert,
            inter_key,
            ["www.example.com"],
            not_before=_NOW - 400 * _ONE_DAY,
            not_after=_NOW - 100 * _ONE_DAY,
        )
        # 名称不匹配叶证书
        self.mismatch_leaf_cert, _ = make_leaf(
            "other.example.com", self.inter_cert, inter_key, ["other.example.com"]
        )

        # 非 CA 的“中间证书”：用它签发的叶证书必须被拒绝
        self.non_ca_cert, non_ca_key = make_leaf(
            "Fake Intermediate", self.inter_cert, inter_key, ["fake.example.com"]
        )
        self.leaf_under_non_ca_cert, _ = make_leaf(
            "www.example.com", self.non_ca_cert, non_ca_key, ["www.example.com"]
        )

        # 超出路径长度：pathlen=0 的中间 CA 又签了一级子 CA
        self.sub_ca_cert, sub_ca_key = make_ca(
            "Illegal Sub CA", path_length=0, issuer_cert=self.inter_cert, issuer_key=inter_key
        )
        self.leaf_under_sub_ca_cert, _ = make_leaf(
            "www.example.com", self.sub_ca_cert, sub_ca_key, ["www.example.com"]
        )

        # 非信任的自签证书
        self.untrusted_self_signed_cert, _ = make_self_signed(
            "www.example.com", ["www.example.com"]
        )

        self.root_pem = pem(self.root_cert)
        self.inter_pem = pem(self.inter_cert)
        self.leaf_pem = pem(self.leaf_cert)
        self.expired_leaf_pem = pem(self.expired_leaf_cert)
        self.mismatch_leaf_pem = pem(self.mismatch_leaf_cert)
        self.non_ca_pem = pem(self.non_ca_cert)
        self.leaf_under_non_ca_pem = pem(self.leaf_under_non_ca_cert)
        self.sub_ca_pem = pem(self.sub_ca_cert)
        self.leaf_under_sub_ca_pem = pem(self.leaf_under_sub_ca_cert)
        self.untrusted_self_signed_pem = pem(self.untrusted_self_signed_cert)
