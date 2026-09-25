"""Signature verification between an issued certificate and its issuer.

Uses Certificate.verify_directly_issued_by on cryptography >= 40, with a
manual primitive-based fallback on older versions. Only standard algorithms
from the cryptography library are used -- nothing is invented here.
"""

from __future__ import annotations

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa, ed448, ed25519


class SignatureError(Exception):
    pass


_WEAK_OIDS = {
    "1.3.14.3.2.29": "SHA1-RSA",
    "1.2.840.113549.1.1.5": "SHA1-RSA",
    "1.2.840.10040.4.3": "SHA1-DSA",
    "1.2.840.10045.4.1": "SHA1-ECDSA",
    "1.2.840.113549.1.1.4": "MD5-RSA",
    "1.3.14.3.2.3": "MD5-RSA",
    "1.2.840.10040.4.1": "DSA",
}


def is_weak_algorithm(oid: str) -> str | None:
    return _WEAK_OIDS.get(oid)


def verify_signature(child: x509.Certificate, issuer: x509.Certificate) -> None:
    """Raise SignatureError unless `child` was signed by `issuer`'s key."""
    if hasattr(child, "verify_directly_issued_by"):
        try:
            child.verify_directly_issued_by(issuer)
            return
        except Exception as exc:  # InvalidSignature, TypeError, UnsupportedAlgorithm
            raise SignatureError(str(exc)) from exc
    _verify_fallback(child, issuer)


def _verify_fallback(child: x509.Certificate, issuer: x509.Certificate) -> None:
    public_key = issuer.public_key()
    tbs = child.tbs_certificate_bytes
    signature = child.signature
    oid = child.signature_algorithm_oid.dotted_string
    try:
        if isinstance(public_key, rsa.RSAPublicKey):
            if oid in ("1.2.840.113549.1.1.1",):  # rsaEncryption (raw PKCS#1)
                digest = None
            else:
                digest = _rsa_hash_for_oid(oid)
            if oid == "1.2.840.113549.1.1.10":  # RSASSA-PSS, parameters carry hash
                try:
                    params = child.signature_hash_algorithm
                except Exception as exc:
                    raise SignatureError(f"cannot read PSS parameters: {exc}") from exc
                public_key.verify(signature, tbs, padding.PSS(
                    mgf=padding.MGF1(params), salt_length=padding.PSS.DIGEST_LENGTH), params)
            elif digest is None:
                public_key.verify(signature, tbs, padding.PKCS1v15(), hashes.SHA256())
            else:
                public_key.verify(signature, tbs, padding.PKCS1v15(), digest)
        elif isinstance(public_key, ec.EllipticCurvePublicKey):
            digest = _ec_hash_for_oid(oid)
            public_key.verify(signature, tbs, ec.ECDSA(digest))
        elif isinstance(public_key, (ed25519.Ed25519PublicKey, ed448.Ed448PublicKey)):
            public_key.verify(signature, tbs)
        else:
            raise SignatureError(f"unsupported issuer key type {type(public_key).__name__}")
    except InvalidSignature:
        raise SignatureError("signature does not verify against issuer public key")
    except Exception as exc:
        if isinstance(exc, SignatureError):
            raise
        raise SignatureError(str(exc)) from exc


def _rsa_hash_for_oid(oid: str) -> hashes.HashAlgorithm:
    table = {
        "1.2.840.113549.1.1.11": hashes.SHA256(),
        "1.2.840.113549.1.1.12": hashes.SHA384(),
        "1.2.840.113549.1.1.13": hashes.SHA512(),
        "1.2.840.113549.1.1.14": hashes.SHA224(),
        "1.2.840.113549.1.1.5": hashes.SHA1(),
        "1.2.840.113549.1.1.4": hashes.MD5(),
    }
    if oid not in table:
        raise SignatureError(f"unsupported RSA signature algorithm OID {oid}")
    return table[oid]


def _ec_hash_for_oid(oid: str) -> hashes.HashAlgorithm:
    table = {
        "1.2.840.10045.4.3.2": hashes.SHA256(),
        "1.2.840.10045.4.3.3": hashes.SHA384(),
        "1.2.840.10045.4.3.4": hashes.SHA512(),
        "1.2.840.10045.4.1": hashes.SHA1(),
    }
    if oid not in table:
        raise SignatureError(f"unsupported ECDSA signature algorithm OID {oid}")
    return table[oid]
