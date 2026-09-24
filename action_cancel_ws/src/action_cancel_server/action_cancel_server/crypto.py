"""Real cryptographic workload for each segment.

Every segment performs genuine, deterministic computations (no stubs, no
randomness):

* ``segment_digest``  : PBKDF2-HMAC-SHA256 over the segment input, with the
  goal id and segment index bound into the salt so identical inputs on
  different segments/goals never collide.
* ``chain_step``      : SHA-256 chaining over the running digest, making the
  final hash a commitment to the exact ordered inputs.
* ``goal_fingerprint``: SHA-256 over the *canonical* goal parameters, used to
  reject "same id, different parameters" attempts.
* ``input_fingerprint``: SHA-256 of a single segment input, persisted so the
  history can prove which input produced a segment without storing raw input.
"""

from __future__ import annotations

import hashlib
import hmac
import json

SEGMENT_DIGEST_BYTES = 32
CHAIN_GENESIS = "0" * 64


def segment_digest(goal_id: str, index: int, text: str, iterations: int) -> str:
    """PBKDF2-HMAC-SHA256 of one segment input.

    The salt binds the goal id (as UTF-8) and the segment index, so swapping
    two inputs produces different per-segment digests.
    """
    if iterations <= 0:
        raise ValueError("pbkdf2_iterations must be positive")
    salt = b"seg-v1|" + goal_id.encode("utf-8") + b"|" + str(index).encode("ascii")
    dk = hashlib.pbkdf2_hmac(
        "sha256", text.encode("utf-8"), salt, iterations, dklen=SEGMENT_DIGEST_BYTES
    )
    return dk.hex()


def chain_step(prev_chain: str, seg_hash: str) -> str:
    """One SHA-256 step of the running chain."""
    return hashlib.sha256((prev_chain + seg_hash).encode("ascii")).hexdigest()


def input_fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def goal_fingerprint(
    goal_id: str, segment_inputs: list[str], iterations: int, recovery_policy: int
) -> str:
    """Deterministic fingerprint of the canonical goal parameters.

    JSON with sort_keys and no whitespace gaps is used purely as a canonical
    byte encoding; both ends are inside this package, so there is no external
    schema dependency.
    """
    payload = json.dumps(
        {
            "goal_id": goal_id,
            "inputs": list(segment_inputs),
            "iter": int(iterations),
            "policy": int(recovery_policy),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def constant_time_equal(a: str, b: str) -> bool:
    """Constant-time hex digest comparison."""
    return hmac.compare_digest(a.encode("ascii"), b.encode("ascii"))
