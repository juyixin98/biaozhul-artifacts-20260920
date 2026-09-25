"""Offline certificate chain construction.

Given a leaf, a pool of untrusted intermediates and the explicit set of
trust anchors, find a path leaf -> ... -> anchor purely by following
issuer/subject names and verifying cryptographic signatures. No network
access, no AIA chasing, no "download missing intermediate".
"""

from __future__ import annotations

from dataclasses import dataclass

from cryptography import x509

from .pemutils import certificate_fingerprint, dedupe
from .signature_check import SignatureError, verify_signature

MAX_PATH_DEPTH = 10


@dataclass
class BuiltChain:
    # Full ordered path: [leaf, intermediate(s), trust anchor]
    path: list[x509.Certificate]
    anchor: x509.Certificate
    # certificate_index is recorded only for errors surfaced from the builder.


class ChainBuildError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _names_equal(name_a: x509.Name, name_b: x509.Name) -> bool:
    return name_a == name_b


def _signed_by(child: x509.Certificate, candidate: x509.Certificate) -> bool:
    if not _names_equal(child.issuer, candidate.subject):
        return False
    try:
        verify_signature(child, candidate)
    except SignatureError:
        return False
    return True


def _aki_matches(child: x509.Certificate, candidate: x509.Certificate) -> bool:
    """If the child carries an AKI, the candidate must carry the matching SKI."""
    try:
        aki = child.extensions.get_extension_for_class(x509.AuthorityKeyIdentifier).value
    except x509.ExtensionNotFound:
        return True
    if aki.key_identifier is None:
        return True  # AKI without keyIdentifier imposes no SKI constraint
    try:
        ski = candidate.extensions.get_extension_for_class(
            x509.SubjectKeyIdentifier
        ).value.digest
    except x509.ExtensionNotFound:
        return False
    return aki.key_identifier == ski


def build_chain(
    leaf: x509.Certificate,
    intermediates: list[x509.Certificate],
    anchors: list[x509.Certificate],
) -> BuiltChain:
    """Depth-first search for a path ending at a trust anchor.

    A chain terminates when the current certificate is itself a trust anchor
    (self-issued root in the trust store) or is signed by one. Trust is
    established by signature plus subject name -- *not* by subject name
    alone, which is what makes the same-name (rogue CA) scenario fail.
    """
    intermediates = dedupe(intermediates)
    anchors = dedupe(anchors)

    anchor_index = {
        (a.subject, certificate_fingerprint(a)): a for a in anchors
    }
    # Quick lookup by name for AKI narrowing; names are never unique keys.
    anchors_by_subject: dict[x509.Name, list[x509.Certificate]] = {}
    for anchor in anchors:
        anchors_by_subject.setdefault(anchor.subject, []).append(anchor)

    intermediates_by_subject: dict[x509.Name, list[x509.Certificate]] = {}
    for cert in intermediates:
        intermediates_by_subject.setdefault(cert.subject, []).append(cert)

    def is_anchor(cert: x509.Certificate) -> x509.Certificate | None:
        return anchor_index.get((cert.subject, certificate_fingerprint(cert)))

    def anchor_signer(cert: x509.Certificate) -> x509.Certificate | None:
        for candidate in anchors_by_subject.get(cert.issuer, []):
            if _aki_matches(cert, candidate) and _signed_by(cert, candidate):
                return candidate
        return None

    def dfs(cert: x509.Certificate, path: list[x509.Certificate], visited: set[bytes]):
        # Invariant: `path` already ends with `cert`.
        # 1) Current cert is itself a trust anchor -> path already complete.
        if is_anchor(cert) is not None:
            return path
        # 2) Current cert is directly signed by a trust anchor.
        signer = anchor_signer(cert)
        if signer is not None:
            return path + [signer]
        # 3) Continue through non-trusted intermediates.
        if len(path) > MAX_PATH_DEPTH:
            return None
        for candidate in intermediates_by_subject.get(cert.issuer, []):
            fp = certificate_fingerprint(candidate)
            if fp in visited:
                continue
            if not _aki_matches(cert, candidate) or not _signed_by(cert, candidate):
                continue
            result = dfs(candidate, path + [candidate], visited | {fp})
            if result is not None:
                return result
        return None

    leaf_fp = certificate_fingerprint(leaf)
    path = dfs(leaf, [leaf], {leaf_fp})
    if path is None:
        raise ChainBuildError(
            "NO_PATH_TO_TRUST_ANCHOR",
            "could not construct a path from the leaf certificate to any "
            "explicitly supplied trust anchor (missing intermediate, unknown "
            "or same-name-but-untrusted root, or signature mismatch)",
        )
    return BuiltChain(path=path, anchor=path[-1])
