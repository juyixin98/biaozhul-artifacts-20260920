"""Offline trust: Ed25519 signatures over canonically-encoded policy bundles.

A signed bundle has the shape::

    {
      "kid": "<hex fingerprint of the trusted public key>",
      "alg": "EdDSA",
      "document": { ...the plain policy object... },
      "sig": "<base64url signature over canonical(document)>"
    }

Only the *document* is signed. Canonical encoding is JCS-style: object keys
sorted lexicographically (UTF-8), no insignificant whitespace. This makes
signatures stable regardless of input key order.

Trust anchors are public keys provisioned out of band into a directory
(``.pem`` files). A bundle is accepted only when its ``kid`` matches a
provisioned key and the Ed25519 signature verifies. There is no network,
CA, or code execution involved — the interpreter never treats the document
as anything but data.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from .errors import PolicyValidationError, TrustError

EDDSA = "EdDSA"
BUNDLE_FIELDS = {"kid", "alg", "document", "sig"}
KEY_FILENAME_SUFFIX = ".pub.pem"


# ---------------------------------------------------------------------------
# Canonical encoding
# ---------------------------------------------------------------------------

def canonical_json(obj: Any) -> bytes:
    """Deterministic JSON encoding of a policy document (JCS conventions).

    * dict keys sorted lexicographically by UTF-8;
    * ``,``/``:`` separators with no whitespace;
    * non-ASCII characters preserved (``ensure_ascii=False``);
    * only JSON types permitted (they already are, documents are validated).
    """
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def key_id(public_key: Ed25519PublicKey) -> str:
    """Stable hex fingerprint for a public key (first 16 bytes SHA-256 DER)."""
    der = public_key.public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    import hashlib

    return hashlib.sha256(der).hexdigest()[:32]


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64u_decode(text: str) -> bytes:
    if not isinstance(text, str):
        raise TrustError("'sig' must be a base64url string")
    pad = "=" * (-len(text) % 4)
    try:
        return base64.urlsafe_b64decode(text + pad)
    except Exception as exc:  # malformed base64
        raise TrustError("malformed base64url signature") from exc


# ---------------------------------------------------------------------------
# Signing (used by tools/sign_policy.py and in tests; never by the server)
# ---------------------------------------------------------------------------

def load_private_key_pem(pem: bytes) -> Ed25519PrivateKey:
    try:
        key = serialization.load_pem_private_key(pem, password=None)
    except Exception as exc:
        raise PolicyValidationError("invalid PEM private key") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise PolicyValidationError("signing key must be Ed25519")
    return key


def sign_document(document: dict[str, Any],
                  private_key: Ed25519PrivateKey) -> dict[str, Any]:
    public = private_key.public_key()
    signature = private_key.sign(canonical_json(document))
    return {
        "kid": key_id(public),
        "alg": EDDSA,
        "document": document,
        "sig": _b64u(signature),
    }


# ---------------------------------------------------------------------------
# Trust store / verification (the server side)
# ---------------------------------------------------------------------------

class TrustStore:
    """A directory-backed set of trusted Ed25519 public keys."""

    def __init__(self, keys: dict[str, Ed25519PublicKey] | None = None):
        self._keys: dict[str, Ed25519PublicKey] = dict(keys or {})

    @classmethod
    def from_directory(cls, path: str | os.PathLike) -> "TrustStore":
        store = cls()
        root = Path(path)
        if not root.exists():
            return store
        for entry in sorted(root.iterdir()):
            # Only public anchors ("*.pub.pem") are trust material; a private
            # key accidentally dropped in the directory must never be loaded.
            if entry.is_file() and entry.name.endswith(KEY_FILENAME_SUFFIX):
                store.add_pem(entry.read_bytes(), source=entry.name)
        return store

    def add_pem(self, pem: bytes, *, source: str = "<pem>") -> str:
        try:
            loaded = serialization.load_pem_public_key(pem)
        except Exception as exc:
            raise TrustError(f"invalid trusted public key in {source}") from exc
        if not isinstance(loaded, Ed25519PublicKey):
            raise TrustError(f"trusted key in {source} must be Ed25519")
        kid = key_id(loaded)
        self._keys[kid] = loaded
        return kid

    @property
    def trusted_kids(self) -> list[str]:
        return sorted(self._keys)

    def verify_bundle(self, bundle: Any) -> dict[str, Any]:
        if not isinstance(bundle, dict):
            raise TrustError("signed bundle must be an object")
        extra = set(bundle) - BUNDLE_FIELDS
        if extra:
            raise TrustError(f"signed bundle has unexpected field(s) {sorted(extra)}")
        kid = bundle.get("kid")
        alg = bundle.get("alg")
        document = bundle.get("document")
        sig = bundle.get("sig")
        if not isinstance(kid, str):
            raise TrustError("bundle 'kid' must be a string")
        if alg != EDDSA:
            raise TrustError(f"unsupported alg {alg!r}; only {EDDSA!r}")
        if not isinstance(document, dict):
            raise TrustError("bundle 'document' must be a JSON object")
        public = self._keys.get(kid)
        if public is None:
            raise TrustError(
                f"no trusted key for kid {kid[:12]}...; "
                f"trusted kids: {self.trusted_kids}"
            )
        signature = _b64u_decode(sig)
        try:
            public.verify(signature, canonical_json(document))
        except InvalidSignature as exc:
            raise TrustError("signature verification failed") from exc
        return document


def generate_keypair() -> tuple[Ed25519PrivateKey, Ed25519PublicKey]:
    """Generate a fresh Ed25519 keypair (CLI helper)."""
    private = Ed25519PrivateKey.generate()
    return private, private.public_key()


def private_key_pem(private_key: Ed25519PrivateKey) -> bytes:
    return private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def public_key_pem(public_key: Ed25519PublicKey) -> bytes:
    return public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
