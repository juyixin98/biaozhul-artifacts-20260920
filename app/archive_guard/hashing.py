"""SHA-256 streaming digest using the ``cryptography`` package."""

from __future__ import annotations

from cryptography.hazmat.primitives import hashes

_CHUNK = 64 * 1024


def sha256_file(path: str) -> str:
    """Return the hex SHA-256 digest of a file, streamed in chunks."""
    digest = hashes.Hash(hashes.SHA256())
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.finalize().hex()


def sha256_bytes(data: bytes) -> str:
    digest = hashes.Hash(hashes.SHA256())
    digest.update(data)
    return digest.finalize().hex()
