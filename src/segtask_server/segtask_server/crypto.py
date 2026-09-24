"""Real cryptographic primitives used by the segmented-task server.

Two independent mechanisms live here:

1. ``compute_segment`` - genuine computational work. Every segment performs
   ``work_units`` rounds of SHA-256 over the chained output of the previous
   segment, so segment outputs are deterministic, reproducible on restart and
   verifiable independently.

2. Event-chain HMAC - an append-only tamper-evident audit log. Every state
   change is an event whose ``event_hash`` is
   ``HMAC-SHA256(server_secret, prev_event_hash || canonical_payload)``.
   Any later edit/deletion of a row breaks the chain (detected by
   :mod:`segtask_server.audit`).

The server secret is a 32-byte random key stored next to the SQLite database
in a file created with mode 0o600. This is a *tamper-evidence* mechanism for
demonstrating integrity, not a substitute for file-system permissions.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import stat
from typing import Any, Mapping

GENESIS = 'GENESIS'


def generate_secret() -> bytes:
    """Return 32 cryptographically secure random bytes (``os.urandom``)."""
    return os.urandom(32)


def load_or_create_secret(key_path: str) -> bytes:
    """Load the HMAC secret from ``key_path``; create it (mode 0o600) if absent.

    Raises PermissionError if an existing key file is readable by group/other
    or is not a regular file - silently proceeding would weaken the chain.
    """
    if os.path.exists(key_path):
        st = os.stat(key_path)
        if not stat.S_ISREG(st.st_mode):
            raise PermissionError(f'secret path is not a regular file: {key_path}')
        if st.st_mode & 0o077:
            raise PermissionError(
                f'secret file {key_path} has loose permissions '
                f'{stat.filemode(st.st_mode)}; expected 0600'
            )
        with open(key_path, 'rb') as fh:
            data = fh.read()
        if len(data) < 32:
            raise ValueError(f'secret file {key_path} is corrupt (<32 bytes)')
        return data[:32]

    secret = generate_secret()
    # Create with 0600 directly: open with O_CREAT|O_EXCL and restricted mode.
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, secret)
    finally:
        os.close(fd)
    os.chmod(key_path, 0o600)
    return secret


def canonical_payload(payload: Mapping[str, Any]) -> str:
    """Deterministic JSON serialization of an event payload (sorted keys)."""
    return json.dumps(payload, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False)


def chain_hash(secret: bytes, prev_hash: str, payload: Mapping[str, Any]) -> str:
    """HMAC-SHA256 over ``prev_hash || canonical(payload)``, hex encoded."""
    msg = prev_hash.encode('utf-8') + b'|' + canonical_payload(payload).encode('utf-8')
    return hmac.new(secret, msg, hashlib.sha256).hexdigest()


def verify_chain_link(secret: bytes, prev_hash: str, payload: Mapping[str, Any],
                      expected_hash: str) -> bool:
    """Constant-time check that a stored event hash matches a recomputed one."""
    return hmac.compare_digest(chain_hash(secret, prev_hash, payload), expected_hash)


def compute_segment(seed: str, work_units: int,
                    should_cancel=None, checkpoint_every: int = 200_000) -> tuple[str, bool]:
    """Run ``work_units`` chained SHA-256 rounds (real CPU work).

    Returns ``(output_hex, canceled)``. If ``should_cancel`` (a zero-arg
    callable) returns True at a checkpoint, iteration stops early and
    ``canceled`` is True - in which case the partial output is NOT meant to be
    committed (the caller discards it).

    ``seed`` is hex (or the genesis marker); chaining means a resumed run with
    the same committed inputs reproduces identical hashes.
    """
    digest = bytes.fromhex(seed) if all(c in '0123456789abcdef' for c in seed) \
        and len(seed) == 64 else seed.encode('utf-8')
    i = 0
    canceled = False
    while i < work_units:
        end = min(i + checkpoint_every, work_units)
        for _ in range(i, end):
            digest = hashlib.sha256(digest).digest()
        i = end
        if should_cancel is not None and should_cancel():
            canceled = True
            break
    return digest.hex(), canceled
