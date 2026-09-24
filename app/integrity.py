"""Real cryptographic integrity verification.

npm lockfiles store tarball integrity as ``<algorithm>-<base64(raw digest)>>``
(see the npm registry ``dist.integrity`` convention / SRI). Verification here
actually computes the digest over the bytes with :mod:`hashlib` and compares
it in constant time (:func:`hmac.compare_digest`). Nothing about this module
touches a network — the tarball must already be vendored inside the bundle.

Tarballs are opened purely as data: ``package/package.json`` is read so the
embedded name/version can be cross-checked, but no script hook is ever run.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import io
import re
import tarfile
from dataclasses import dataclass

SUPPORTED_STRONG = {"sha512", "sha384", "sha256"}
KNOWN_WEAK = {"sha1"}
_INTEGRITY_RE = re.compile(r"^(?P<algo>[a-z0-9]+)-(?P<b64>[A-Za-z0-9+/]+[=]{0,2})$")
_EXPECTED_DIGEST_LEN = {"sha512": 64, "sha384": 48, "sha256": 32, "sha1": 20}


class IntegrityError(ValueError):
    """Raised when an integrity string is malformed or verification fails."""


@dataclass(frozen=True)
class IntegrityValue:
    algorithm: str
    digest: bytes
    raw: str

    @property
    def is_weak(self) -> bool:
        return self.algorithm in KNOWN_WEAK


def parse_integrity(raw: object) -> IntegrityValue:
    if not isinstance(raw, str) or not raw.strip():
        raise IntegrityError("integrity field is missing or not a string")
    text = raw.strip()
    # Multiple SRI hashes separated by whitespace are legal in a browser
    # context but npm lockfiles always pin exactly one; refuse ambiguity.
    if len(text.split()) != 1:
        raise IntegrityError("multiple integrity hashes are not supported")
    match = _INTEGRITY_RE.match(text)
    if not match:
        raise IntegrityError(f"malformed integrity string {text!r}")
    algo = match.group("algo")
    if algo not in SUPPORTED_STRONG and algo not in KNOWN_WEAK:
        raise IntegrityError(f"unsupported integrity algorithm {algo!r}")
    try:
        digest = base64.b64decode(match.group("b64"), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise IntegrityError(f"integrity digest is not valid base64: {exc}") from exc
    expected_len = _EXPECTED_DIGEST_LEN.get(algo)
    if expected_len and len(digest) != expected_len:
        raise IntegrityError(
            f"{algo} digest must be {expected_len} bytes, got {len(digest)}"
        )
    return IntegrityValue(algorithm=algo, digest=digest, raw=text)


def compute_digest(algorithm: str, blob: bytes) -> bytes:
    if algorithm not in SUPPORTED_STRONG | KNOWN_WEAK:
        raise IntegrityError(f"unsupported hash algorithm {algorithm!r}")
    return hashlib.new(algorithm, blob).digest()


def verify_tarball(integrity: str, blob: bytes) -> IntegrityValue:
    """Parse the integrity string and check it against ``blob``.

    Raises :class:`IntegrityError` on mismatch; returns the parsed value on
    success so callers can flag weak algorithms.
    """
    parsed = parse_integrity(integrity)
    actual = compute_digest(parsed.algorithm, blob)
    if not hmac.compare_digest(actual, parsed.digest):
        raise IntegrityError(
            f"{parsed.algorithm} digest mismatch: "
            f"lock expects {base64.b64encode(parsed.digest).decode()}, "
            f"content has {base64.b64encode(actual).decode()}"
        )
    return parsed


# ---------------------------------------------------------------------------
# Tarball inspection (no execution)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TarballManifest:
    name: str | None
    version: str | None
    has_install_scripts: bool
    scripts: dict[str, str]
    package_json_raw: bytes | None


_LIFECYCLE_HOOKS = frozenset(
    {
        "preinstall",
        "install",
        "postinstall",
        "preprepare",
        "prepare",
        "postprepare",
        "prepublish",
    }
)


def inspect_tarball(blob: bytes) -> TarballManifest:
    """Open a registry tarball and read its ``package/package.json``.

    Registry packages always use a top-level ``package/`` directory. The file
    is parsed only for metadata; its ``scripts`` are reported, never invoked.
    """
    try:
        tf = tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz")
    except (tarfile.TarError, OSError) as exc:
        raise IntegrityError(f"tarball is not a valid gzip tar: {exc}") from exc
    try:
        members = tf.getnames()
    except tarfile.TarError as exc:
        tf.close()
        raise IntegrityError(f"tarball has an unreadable member table: {exc}") from exc

    candidates = [m for m in members if m in ("package/package.json", "package.json")]
    if not candidates:
        tf.close()
        raise IntegrityError("tarball contains no package/package.json manifest")
    chosen = candidates[0]
    member = tf.getmember(chosen)
    if not member.isfile():
        tf.close()
        raise IntegrityError("tarball manifest is not a regular file")
    raw = tf.extractfile(member).read()
    tf.close()

    import json

    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IntegrityError(f"tarball manifest is invalid JSON: {exc}") from exc
    if not isinstance(manifest, dict):
        raise IntegrityError("tarball manifest root must be an object")

    scripts = manifest.get("scripts") or {}
    if not isinstance(scripts, dict):
        raise IntegrityError("tarball manifest 'scripts' must be an object")
    hooks = {k: str(v) for k, v in scripts.items() if k in _LIFECYCLE_HOOKS}
    return TarballManifest(
        name=manifest.get("name") if isinstance(manifest.get("name"), str) else None,
        version=(
            manifest.get("version")
            if isinstance(manifest.get("version"), str)
            else None
        ),
        has_install_scripts=bool(hooks),
        scripts=hooks,
        package_json_raw=raw,
    )
