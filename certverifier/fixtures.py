"""Local generation of test keys and X.509 chains.

Everything here is generated on the local machine with the `cryptography`
library -- no production accounts, no imported real-world roots. The
datetimes are fixed so tests are deterministic and the "verification
moment" is always an explicit input.

Timeline used across scenarios (UTC):

    root / intermediates valid:   2020-01-01 .. 2030-01-01
    valid leaf:                   2025-01-01 .. 2026-01-01
    verification moment:          2025-06-01  (valid)
    expired-leaf moment:          2027-06-01
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

UTC = timezone.utc

# Fixed timeline
ROOT_START = datetime(2020, 1, 1, tzinfo=UTC)
ROOT_END = datetime(2030, 1, 1, tzinfo=UTC)
LEAF_START = datetime(2025, 1, 1, tzinfo=UTC)
LEAF_END = datetime(2026, 1, 1, tzinfo=UTC)

VALID_MOMENT = datetime(2025, 6, 1, 12, 0, tzinfo=UTC)
EXPIRED_MOMENT = datetime(2027, 6, 1, 12, 0, tzinfo=UTC)
BEFORE_LEAF_MOMENT = datetime(2024, 6, 1, 12, 0, tzinfo=UTC)


def _serial() -> int:
    # Random 64-bit serial, fresh on every generation run.
    return x509.random_serial_number()


def new_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _name(cn: str, org: str = "Offline Test CA") -> x509.Name:
    return x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, cn),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, org),
        ]
    )


def make_root(
    cn: str = "Offline Test Root CA",
    *,
    path_length: Optional[int] = None,
    key: Optional[rsa.RSAPrivateKey] = None,
    not_before: datetime = ROOT_START,
    not_after: datetime = ROOT_END,
    org: str = "Offline Test CA",
) -> tuple[x509.Certificate, rsa.RSAPrivateKey]:
    """Create a self-signed root CA certificate + private key."""
    key = key or new_key()
    subject = issuer = _name(cn, org)
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(_serial())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .add_extension(x509.BasicConstraints(ca=True, path_length=path_length), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_cert_sign=True,
                crl_sign=True,
                key_agreement=False,
                content_commitment=False,
                data_encipherment=False,
                key_encipherment=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False
        )
    )
    return builder.sign(key, hashes.SHA256()), key


def _apply_extensions(builder: x509.CertificateBuilder, *, ca: bool, path_length,
                      server_auth, client_auth, dns_names, ip_addresses, leaf_usage):
    if ca:
        builder = builder.add_extension(
            x509.BasicConstraints(ca=True, path_length=path_length), critical=True
        )
        builder = builder.add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_cert_sign=True,
                crl_sign=True,
                key_agreement=False,
                content_commitment=False,
                data_encipherment=False,
                key_encipherment=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
    else:
        builder = builder.add_extension(
            x509.BasicConstraints(ca=False, path_length=None), critical=True
        )
        if leaf_usage:
            builder = builder.add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    key_encipherment=True,
                    key_agreement=False,
                    content_commitment=False,
                    data_encipherment=False,
                    key_cert_sign=False,
                    crl_sign=False,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
        if dns_names or ip_addresses:
            general_names = [x509.DNSName(n) for n in dns_names]
            import ipaddress as _ip
            for value in ip_addresses:
                general_names.append(x509.IPAddress(_ip.ip_address(value)))
            builder = builder.add_extension(
                x509.SubjectAlternativeName(general_names), critical=False
            )
        eku = []
        if server_auth:
            eku.append(ExtendedKeyUsageOID.SERVER_AUTH)
        if client_auth:
            eku.append(ExtendedKeyUsageOID.CLIENT_AUTH)
        if eku:
            builder = builder.add_extension(x509.ExtendedKeyUsage(eku), critical=False)
    return builder


def issue(
    issuer_cert: x509.Certificate,
    issuer_key,
    cn: str,
    *,
    ca: bool = False,
    path_length: Optional[int] = None,
    server_auth: bool = True,
    client_auth: bool = False,
    dns_names: tuple[str, ...] = (),
    ip_addresses: tuple[str, ...] = (),
    key: Optional[rsa.RSAPrivateKey] = None,
    not_before: datetime = ROOT_START,
    not_after: datetime = ROOT_END,
    leaf_usage: bool = True,
    org: str = "Offline Test CA",
) -> tuple[x509.Certificate, rsa.RSAPrivateKey]:
    """Issue a CA or leaf certificate signed by `issuer_cert`."""
    key = key or new_key()
    builder = (
        x509.CertificateBuilder()
        .subject_name(_name(cn, org))
        .issuer_name(issuer_cert.subject)
        .public_key(key.public_key())
        .serial_number(_serial())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False
        )
    )
    try:
        issuer_ski = issuer_cert.extensions.get_extension_for_class(
            x509.SubjectKeyIdentifier
        ).value
        builder = builder.add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(issuer_ski),
            critical=False,
        )
    except x509.ExtensionNotFound:
        builder = builder.add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_cert.public_key()),
            critical=False,
        )
    builder = _apply_extensions(
        builder,
        ca=ca,
        path_length=path_length,
        server_auth=server_auth,
        client_auth=client_auth,
        dns_names=dns_names,
        ip_addresses=ip_addresses,
        leaf_usage=leaf_usage,
    )
    return builder.sign(issuer_key, hashes.SHA256()), key


@dataclass
class Scenario:
    """One generated chain scenario used by fixtures, CLI examples and tests."""

    name: str
    description: str
    leaf: x509.Certificate
    leaf_key: rsa.RSAPrivateKey
    intermediates: list[x509.Certificate]
    anchors: list[x509.Certificate]
    hostname: Optional[str]
    verification_time: datetime
    purpose: str = "serverAuth"
    expect_valid: bool = True
    expected_codes: set[str] = field(default_factory=set)
    extra_intermediates: list[x509.Certificate] = field(default_factory=list)  # decoys
    # Roots present in the scenario data but deliberately NOT trusted; tests
    # use these to show what trusting them (or matching them by name) would do.
    untrusted_roots: list[x509.Certificate] = field(default_factory=list)

    @property
    def all_intermediates(self) -> list[x509.Certificate]:
        return self.intermediates + self.extra_intermediates


def _good() -> Scenario:
    root, root_key = make_root("Offline Test Root CA")
    inter, inter_key = make_chain_ca(root, root_key, "Offline Test Intermediate CA")
    leaf, leaf_key = issue(
        inter, inter_key, "example.test",
        dns_names=("example.test", "www.example.test"),
        not_before=LEAF_START, not_after=LEAF_END, org="Example Test Org",
    )
    return Scenario(
        name="good",
        description="Valid chain: root -> intermediate -> leaf, in date, "
        "serverAuth EKU, SAN example.test, verified at 2025-06-01.",
        leaf=leaf, leaf_key=leaf_key, intermediates=[inter], anchors=[root],
        hostname="example.test", verification_time=VALID_MOMENT, expect_valid=True,
    )


def make_chain_ca(root, root_key, cn, *, path_length=None, not_before=ROOT_START,
                  not_after=ROOT_END, key=None):
    cert, key = issue(
        root, root_key, cn, ca=True, path_length=path_length,
        server_auth=False, not_before=not_before, not_after=not_after, key=key,
    )
    return cert, key


def _wildcard() -> Scenario:
    root, root_key = make_root("Offline Test Root CA")
    inter, inter_key = make_chain_ca(root, root_key, "Offline Test Intermediate CA")
    leaf, leaf_key = issue(
        inter, inter_key, "wildcard.example.test",
        dns_names=("*.example.test",),
        not_before=LEAF_START, not_after=LEAF_END,
    )
    return Scenario(
        name="wildcard",
        description="Wildcard SAN *.example.test must match one label only.",
        leaf=leaf, leaf_key=leaf_key, intermediates=[inter], anchors=[root],
        hostname="api.example.test", verification_time=VALID_MOMENT, expect_valid=True,
    )


def _expired_leaf() -> Scenario:
    root, root_key = make_root("Offline Test Root CA")
    inter, inter_key = make_chain_ca(root, root_key, "Offline Test Intermediate CA")
    leaf, leaf_key = issue(
        inter, inter_key, "expired.example.test",
        dns_names=("expired.example.test",),
        not_before=LEAF_START, not_after=LEAF_END,
    )
    return Scenario(
        name="expired",
        description="Expired leaf: same chain verified at 2027-06-01, after "
        "the leaf notAfter (2026-01-01).",
        leaf=leaf, leaf_key=leaf_key, intermediates=[inter], anchors=[root],
        hostname="expired.example.test", verification_time=EXPIRED_MOMENT,
        expect_valid=False, expected_codes={"EXPIRED"},
    )


def _not_before_leaf() -> Scenario:
    root, root_key = make_root("Offline Test Root CA")
    inter, inter_key = make_chain_ca(root, root_key, "Offline Test Intermediate CA")
    leaf, leaf_key = issue(
        inter, inter_key, "future.example.test",
        dns_names=("future.example.test",),
        not_before=LEAF_START, not_after=LEAF_END,
    )
    return Scenario(
        name="not_yet_valid",
        description="Leaf notBefore is 2025-01-01 but verification runs 2024-06-01.",
        leaf=leaf, leaf_key=leaf_key, intermediates=[inter], anchors=[root],
        hostname="future.example.test", verification_time=BEFORE_LEAF_MOMENT,
        expect_valid=False, expected_codes={"NOT_YET_VALID"},
    )


def _path_length() -> Scenario:
    # Root limits path length to 0: no intermediate CA may sit below it.
    root, root_key = make_root("Offline Test Root CA (pathLen 0)", path_length=0)
    inter, inter_key = make_chain_ca(root, root_key, "Offline Test Intermediate CA")
    leaf, leaf_key = issue(
        inter, inter_key, "pathlen.example.test",
        dns_names=("pathlen.example.test",),
        not_before=LEAF_START, not_after=LEAF_END,
    )
    return Scenario(
        name="path_length_violation",
        description="Root has basicConstraints pathLenConstraint=0 but a CA "
        "intermediate is present, violating RFC 5280 4.2.1.9.",
        leaf=leaf, leaf_key=leaf_key, intermediates=[inter], anchors=[root],
        hostname="pathlen.example.test", verification_time=VALID_MOMENT,
        expect_valid=False, expected_codes={"PATH_LENGTH_VIOLATION"},
    )


def _non_ca_intermediate() -> Scenario:
    root, root_key = make_root("Offline Test Root CA")
    # A leaf-shaped (CA:FALSE) certificate wrongly used as an issuer.
    fake_inter, fake_inter_key = issue(
        root, root_key, "Not A CA",
        ca=False, server_auth=False, leaf_usage=False,
    )
    leaf, leaf_key = issue(
        fake_inter, fake_inter_key, "badsign.example.test",
        dns_names=("badsign.example.test",),
        not_before=LEAF_START, not_after=LEAF_END,
    )
    return Scenario(
        name="non_ca_intermediate",
        description="Intermediate certificate has basicConstraints CA:FALSE "
        "(and no keyCertSign); it must not be used to sign/issue the leaf.",
        leaf=leaf, leaf_key=leaf_key, intermediates=[fake_inter], anchors=[root],
        hostname="badsign.example.test", verification_time=VALID_MOMENT,
        expect_valid=False, expected_codes={"NOT_A_CA"},
    )


def _rogue_same_name_root() -> Scenario:
    # Genuine trusted root.
    real_root, real_root_key = make_root("Same Name Root CA")
    inter, inter_key = make_chain_ca(real_root, real_root_key, "Offline Test Intermediate CA")
    leaf, leaf_key = issue(
        inter, inter_key, "real.example.test",
        dns_names=("real.example.test",),
        not_before=LEAF_START, not_after=LEAF_END,
    )

    # Rogue root with the IDENTICAL subject DN, different key.
    rogue_root, _rogue_key = make_root("Same Name Root CA")
    rogue_inter, rogue_inter_key = make_chain_ca(
        rogue_root, _rogue_key, "Rogue Intermediate CA"
    )
    rogue_leaf, rogue_leaf_key = issue(
        rogue_inter, rogue_inter_key, "evil.example.test",
        dns_names=("evil.example.test",),
        not_before=LEAF_START, not_after=LEAF_END,
    )

    # Present both chains; only the real root is in the trust store.
    return Scenario(
        name="same_name_rogue_root",
        description="A rogue CA shares the trusted root's exact subject DN but "
        "has a different key. Name matching alone would be fooled; signature/"
        "SPKI validation rejects the rogue chain.",
        leaf=rogue_leaf, leaf_key=rogue_leaf_key,
        intermediates=[inter, rogue_inter], anchors=[real_root],
        hostname="evil.example.test", verification_time=VALID_MOMENT,
        expect_valid=False, expected_codes={"NO_PATH_TO_TRUST_ANCHOR"},
        untrusted_roots=[rogue_root],
    )

def _same_name_intermediate() -> Scenario:
    root, root_key = make_root("Offline Test Root CA")
    # Two intermediates with the same subject DN, one genuinely signed, one
    # self-asserted/rogue (signed by an unrelated rogue key).
    good_inter, good_inter_key = make_chain_ca(root, root_key, "Shared Name Intermediate")
    leaf, leaf_key = issue(
        good_inter, good_inter_key, "good-san.example.test",
        dns_names=("good-san.example.test",),
        not_before=LEAF_START, not_after=LEAF_END,
    )

    rogue_key = new_key()
    rogue_root, rogue_root_key = make_root("Unrelated Rogue Root", key=rogue_key)
    # Rogue intermediate with same CN as the good one, signed by rogue root.
    rogue_inter, rogue_inter_key = issue(
        rogue_root, rogue_root_key, "Shared Name Intermediate",
        ca=True, server_auth=False,
    )
    rogue_leaf, rogue_leaf_key = issue(
        rogue_inter, rogue_inter_key, "evil2.example.test",
        dns_names=("evil2.example.test",),
        not_before=LEAF_START, not_after=LEAF_END,
    )

    return Scenario(
        name="same_name_intermediate",
        description="Two intermediates share a subject DN; only one chains to "
        "the anchor. The builder must follow signatures, not names.",
        leaf=rogue_leaf, leaf_key=rogue_leaf_key,
        intermediates=[good_inter, rogue_inter], anchors=[root],
        hostname="evil2.example.test", verification_time=VALID_MOMENT,
        expect_valid=False, expected_codes={"NO_PATH_TO_TRUST_ANCHOR"},
        extra_intermediates=[],
        untrusted_roots=[rogue_root],
    )


def _hostname_mismatch() -> Scenario:
    root, root_key = make_root("Offline Test Root CA")
    inter, inter_key = make_chain_ca(root, root_key, "Offline Test Intermediate CA")
    leaf, leaf_key = issue(
        inter, inter_key, "example.test",
        dns_names=("example.test",),
        not_before=LEAF_START, not_after=LEAF_END,
    )
    return Scenario(
        name="hostname_mismatch",
        description="Chain is valid, but the requested host 'attacker.test' "
        "is not in the certificate SAN.",
        leaf=leaf, leaf_key=leaf_key, intermediates=[inter], anchors=[root],
        hostname="attacker.test", verification_time=VALID_MOMENT,
        expect_valid=False, expected_codes={"HOSTNAME_MISMATCH"},
    )


def _eku_mismatch() -> Scenario:
    root, root_key = make_root("Offline Test Root CA")
    inter, inter_key = make_chain_ca(root, root_key, "Offline Test Intermediate CA")
    leaf, leaf_key = issue(
        inter, inter_key, "clientonly.example.test",
        server_auth=False, client_auth=True,
        dns_names=("clientonly.example.test",),
        not_before=LEAF_START, not_after=LEAF_END,
    )
    return Scenario(
        name="eku_mismatch",
        description="Leaf has only clientAuth EKU; verifying for serverAuth fails.",
        leaf=leaf, leaf_key=leaf_key, intermediates=[inter], anchors=[root],
        hostname="clientonly.example.test", verification_time=VALID_MOMENT,
        expect_valid=False, expected_codes={"EKU_MISMATCH"},
    )


def _untrusted_root() -> Scenario:
    root, root_key = make_root("Offline Test Root CA")
    other_root, other_key = make_root("Some Other Root")
    inter, inter_key = make_chain_ca(other_root, other_key, "Other Intermediate")
    leaf, leaf_key = issue(
        inter, inter_key, "other.example.test",
        dns_names=("other.example.test",),
        not_before=LEAF_START, not_after=LEAF_END,
    )
    return Scenario(
        name="untrusted_root",
        description="Chain is internally valid but its root is not in the "
        "explicitly supplied trust store.",
        leaf=leaf, leaf_key=leaf_key, intermediates=[inter],
        anchors=[root],  # wrong anchor supplied
        hostname="other.example.test", verification_time=VALID_MOMENT,
        expect_valid=False, expected_codes={"NO_PATH_TO_TRUST_ANCHOR"},
    )


def _expired_intermediate() -> Scenario:
    root, root_key = make_root("Offline Test Root CA")
    expired_inter, expired_key = make_chain_ca(
        root, root_key, "Expired Intermediate CA",
        not_before=datetime(2020, 1, 1, tzinfo=UTC),
        not_after=datetime(2022, 1, 1, tzinfo=UTC),
    )
    leaf, leaf_key = issue(
        expired_inter, expired_key, "expiredint.example.test",
        dns_names=("expiredint.example.test",),
        not_before=LEAF_START, not_after=LEAF_END,
    )
    return Scenario(
        name="expired_intermediate",
        description="Intermediate CA already expired at 2022-01-01; verified "
        "2025-06-01 -> EXPIRED on the intermediate.",
        leaf=leaf, leaf_key=leaf_key, intermediates=[expired_inter], anchors=[root],
        hostname="expiredint.example.test", verification_time=VALID_MOMENT,
        expect_valid=False, expected_codes={"EXPIRED"},
    )


def build_all_scenarios() -> dict[str, Scenario]:
    builders = [
        ("good", _good),
        ("wildcard", _wildcard),
        ("expired", _expired_leaf),
        ("not_yet_valid", _not_before_leaf),
        ("path_length_violation", _path_length),
        ("non_ca_intermediate", _non_ca_intermediate),
        ("same_name_rogue_root", _rogue_same_name_root),
        ("same_name_intermediate", _same_name_intermediate),
        ("hostname_mismatch", _hostname_mismatch),
        ("eku_mismatch", _eku_mismatch),
        ("untrusted_root", _untrusted_root),
        ("expired_intermediate", _expired_intermediate),
    ]
    return {name: builder() for name, builder in builders}
