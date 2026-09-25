"""RFC 5280 style offline path validation.

The caller supplies everything the policy depends on:

* the leaf certificate,
* untrusted intermediates,
* the explicit trust anchors,
* the verification moment (no reliance on wall-clock "now" by accident),
* the key purpose (serverAuth/clientAuth/any),
* and, optionally, the hostname/IP to match against the SAN.

The implementation follows the checks of RFC 5280 section 6 (signature
verification, validity dates, basicConstraints & pathLen, keyUsage,
extKeyUsage, name chaining) with the cryptographic primitives of the
`cryptography` library. It deliberately omits everything that needs
network access: CRL / OCSP / OCSP-must-staple / CRLite are never queried
and every result states that revocation was not checked.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from .chain_builder import ChainBuildError, build_chain
from .hostnames import check_hostname
from .models import EKU_OID, Finding, Purpose, VerificationOptions, VerificationResult
from .pemutils import _not_after, _not_before, certificate_summary
from .signature_check import SignatureError, is_weak_algorithm, verify_signature


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _basic_constraints(cert: x509.Certificate):
    try:
        ext = cert.extensions.get_extension_for_class(x509.BasicConstraints)
        return ext.value.ca, ext.value.path_length
    except x509.ExtensionNotFound:
        return None, None


def _key_usage(cert: x509.Certificate):
    try:
        return cert.extensions.get_extension_for_class(x509.KeyUsage).value
    except x509.ExtensionNotFound:
        return None


def _eku(cert: x509.Certificate):
    try:
        return cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    except x509.ExtensionNotFound:
        return None


def verify_chain(
    leaf: x509.Certificate,
    intermediates: list[x509.Certificate],
    anchors: list[x509.Certificate],
    *,
    verification_time: datetime,
    purpose: Purpose | str = Purpose.SERVER_AUTH,
    hostname: Optional[str] = None,
    options: Optional[VerificationOptions] = None,
) -> VerificationResult:
    options = options or VerificationOptions()
    purpose = Purpose.parse(purpose)
    verification_time = _utc(verification_time)

    result = VerificationResult(
        valid=False,
        verification_time=verification_time,
        purpose=purpose.value,
        hostname=hostname,
    )

    # ------------------------------------------------------------------ chain
    try:
        built = build_chain(leaf, intermediates, anchors)
    except ChainBuildError as exc:
        result.findings.append(
            Finding(
                code=exc.code,
                message=exc.message,
                certificate_index=0,
                subject=leaf.subject.rfc4514_string(),
            )
        )
        result.chain = [certificate_summary(leaf, index=0)]
        result.notes.append(
            "Revocation status NOT checked (offline mode); path construction failed."
        )
        return result

    path = built.path
    result.chain = [
        certificate_summary(c, index=i, is_anchor=(i == len(path) - 1))
        for i, c in enumerate(path)
    ]
    result.trust_anchor = certificate_summary(built.anchor, is_anchor=True)

    findings: list[Finding] = []

    def add(code, message, cert, index):
        findings.append(
            Finding(
                code=code,
                message=message,
                certificate_index=index,
                subject=cert.subject.rfc4514_string(),
            )
        )

    # ------------------------------------------------- RFC 5280 6.1 / 6.2 loop
    non_anchor_count = len(path) - 1  # certificates below the trust anchor

    for index, cert in enumerate(path):
        is_anchor = index == len(path) - 1
        summary_subject = cert.subject.rfc4514_string()

        # (a) validity dates
        not_before = _not_before(cert)
        not_after = _not_after(cert)
        if not is_anchor or options.check_anchor_validity:
            if verification_time < not_before:
                add(
                    "NOT_YET_VALID",
                    f"certificate not valid before {not_before.isoformat()} "
                    f"(verification time {verification_time.isoformat()})",
                    cert,
                    index,
                )
            if verification_time > not_after:
                add(
                    "EXPIRED",
                    f"certificate expired at {not_after.isoformat()} "
                    f"(verification time {verification_time.isoformat()})",
                    cert,
                    index,
                )
        else:
            if verification_time < not_before or verification_time > not_after:
                result.notes.append(
                    f"Trust anchor '{summary_subject}' validity dates "
                    f"({not_before.date()}..{not_after.date()}) do not cover "
                    "the verification time; not enforced (anchors are trusted "
                    "by provision). Set check_anchor_validity=true to enforce."
                )

        # (b) weak signature algorithms
        weak = is_weak_algorithm(cert.signature_algorithm_oid.dotted_string)
        if weak and not options.allow_weak_signature_algorithms:
            add(
                "WEAK_SIGNATURE_ALGORITHM",
                f"certificate is signed with weak algorithm {weak} "
                f"(OID {cert.signature_algorithm_oid.dotted_string})",
                cert,
                index,
            )

        # (c) issuer name chaining + signature (re-verify for defence in depth;
        # the builder only proved a path exists).
        if not is_anchor:
            issuer = path[index + 1]
            if cert.issuer != issuer.subject:
                add(
                    "ISSUER_NAME_MISMATCH",
                    "certificate issuer name does not match next certificate subject",
                    cert,
                    index,
                )
            else:
                try:
                    verify_signature(cert, issuer)
                except SignatureError as exc:
                    add(
                        "BAD_SIGNATURE",
                        f"signature does not verify against issuer public key: {exc}",
                        cert,
                        index,
                    )

        # (d) basicConstraints
        # Path layout: index 0 = leaf, indices 1..non_anchor_count-1 =
        # subordinate CA intermediates, index non_anchor_count = anchor.
        is_subordinate_ca = 0 < index < non_anchor_count
        is_ca, path_len = _basic_constraints(cert)
        if is_anchor:
            # Trust anchors should be CAs; flag a misconfigured trust store.
            if is_ca is False:
                add(
                    "TRUST_ANCHOR_NOT_CA",
                    "trust anchor certificate has basicConstraints CA:FALSE",
                    cert,
                    index,
                )
        elif is_subordinate_ca:
            # A certificate used to sign another certificate MUST be a CA.
            if is_ca is None:
                add(
                    "MISSING_BASIC_CONSTRAINTS",
                    "intermediate certificate lacks basicConstraints and "
                    "cannot act as a CA",
                    cert,
                    index,
                )
            elif is_ca is False:
                add(
                    "NOT_A_CA",
                    "certificate used to issue another certificate has "
                    "basicConstraints CA:FALSE",
                    cert,
                    index,
                )
        else:
            # End-entity certificate: CA:TRUE would be wrong here.
            if is_ca is True:
                add(
                    "LEAF_IS_CA",
                    "end-entity certificate asserts basicConstraints CA:TRUE",
                    cert,
                    index,
                )

        # (e) pathLenConstraint (RFC 5280 4.2.1.9): the maximum number of
        # non-self-issued intermediate CA certificates allowed to follow THIS
        # certificate on the path toward the leaf. For a CA at position
        # `index` (>=1), the CA certs between it and the leaf number
        # `index - 1` (e.g. root index 2 with one intermediate below it: 1).
        if index >= 1 and is_ca is True and path_len is not None:
            following_cas = index - 1
            if following_cas > path_len:
                add(
                    "PATH_LENGTH_VIOLATION",
                    f"pathLenConstraint={path_len} on this CA but "
                    f"{following_cas} CA certificate(s) follow it before "
                    "the leaf",
                    cert,
                    index,
                )

        # (f) keyUsage
        usage = _key_usage(cert)
        if usage is not None:
            if is_subordinate_ca:
                # A CA certificate used to verify signatures needs keyCertSign.
                try:
                    if usage.key_cert_sign is False:
                        add(
                            "KEY_USAGE_NO_KEY_CERT_SIGN",
                            "intermediate CA certificate keyUsage does not "
                            "include keyCertSign",
                            cert,
                            index,
                        )
                except (UnsupportedAlgorithm, ValueError):
                    pass
            elif index == 0:
                # Leaf: digitalSignature required for TLS-style auth.
                try:
                    if usage.digital_signature is False:
                        add(
                            "KEY_USAGE_NO_DIGITAL_SIGNATURE",
                            "end-entity certificate keyUsage does not include "
                            "digitalSignature",
                            cert,
                            index,
                        )
                except (UnsupportedAlgorithm, ValueError):
                    pass

        # (g) extKeyUsage -- only meaningful on the leaf unless it is
        # narrowed by an intermediate (RFC 5280: an EKU in a CA cert acts as
        # a further constraint on the whole path).
        if purpose is not Purpose.ANY:
            eku_ext = _eku(cert)
            if eku_ext is not None and not is_anchor:
                required_oid = EKU_OID[purpose]
                present = {oid.dotted_string for oid in eku_ext}
                if "2.5.29.37.0" not in present and required_oid not in present:
                    add(
                        "EKU_MISMATCH",
                        f"extKeyUsage does not include {purpose.value} "
                        f"(OID {required_oid}) and is not anyExtendedKeyUsage",
                        cert,
                        index,
                    )

    # --------------------------------------------------------- leaf hostname
    leaf_cert = path[0]
    if purpose is Purpose.SERVER_AUTH or purpose is Purpose.CLIENT_AUTH:
        if not hostname:
            result.notes.append(
                "No hostname supplied; SAN/CN hostname verification skipped. "
                "Pass a hostname to bind the certificate to an endpoint identity."
            )
        else:
            error = check_hostname(
                leaf_cert, hostname, allow_cn_fallback=options.allow_cn_hostname
            )
            if error:
                add(
                    error,
                    f"hostname {hostname!r} does not match the end-entity "
                    "certificate (checked SAN dNSName/iPAddress only, "
                    "CN fallback disabled)"
                    if error != "HOSTNAME_NO_SAN"
                    else f"hostname {hostname!r} cannot be verified: certificate "
                    "has no subjectAltName and CN fallback is disabled",
                    leaf_cert,
                    0,
                )

    # ----------------------------- public key sanity (never trust bad inputs)
    for index, cert in enumerate(path):
        key = cert.public_key()
        if isinstance(key, rsa.RSAPublicKey) and key.key_size < 2048:
            add(
                "WEAK_KEY",
                f"RSA key size {key.key_size} < 2048 bits",
                cert,
                index,
            )
        if isinstance(key, ec.EllipticCurvePublicKey) and key.curve.key_size < 224:
            add(
                "WEAK_KEY",
                f"EC curve {key.curve.name} provides only {key.curve.key_size} bits",
                cert,
                index,
            )

    result.findings = findings
    result.valid = not findings
    result.notes.append(
        "Revocation status NOT checked: no CRL, OCSP or stapled response was "
        "inspected (offline mode by design)."
    )
    if not intermediates and non_anchor_count > 1:
        # Path could only be built if intermediates were provided; note when
        # a self-signed-looking chain was validated directly.
        pass
    return result
