"""Test-only signing utilities using REAL Ed25519 keys.

This module exists because the service only accepts offline votes that carry a
valid signature over the chain-bound canonical message. Tests and example
generators use these helpers to produce genuine signatures — including
tampered/replayed/cross-chain ones to prove the service rejects them.
"""
from __future__ import annotations

import hashlib
import secrets

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from . import domain


def new_validator() -> dict:
    """Generate a fresh validator keypair and its derived address."""
    sk = domain.generate_private_key()
    pub = domain.public_hex(sk)
    return {
        "private_key": sk,
        "public_key": pub,
        "validator_address": domain.derive_address(bytes.fromhex(pub)),
    }


def make_vote(
    validator: dict,
    *,
    chain_id: str,
    height: int,
    round: int,
    vote_type: str,
    block_hash: str | None = None,
) -> dict:
    """Build and genuinely sign a valid offline vote envelope."""
    if block_hash is None:
        block_hash = hashlib.sha256(secrets.token_bytes(32)).hexdigest()
    fields = domain.VoteFields(
        chain_id=chain_id,
        validator_address=validator["validator_address"],
        height=height,
        round=round,
        vote_type=vote_type,
        block_hash=block_hash,
    )
    signature = domain.sign_vote(validator["private_key"], fields)
    return {
        "chain_id": fields.chain_id,
        "validator_address": fields.validator_address,
        "height": fields.height,
        "round": fields.round,
        "vote_type": fields.vote_type,
        "block_hash": fields.block_hash,
        "public_key": validator["public_key"],
        "signature": signature,
    }


def tamper(envelope: dict, **changes) -> dict:
    """Copy an envelope with fields changed (but signature kept). Invalid."""
    out = dict(envelope)
    out.update(changes)
    return out


def resign_with_other_key(envelope: dict, other: dict) -> dict:
    """Re-sign a body using a different key but keep the claimed address."""
    fields = domain.VoteFields(
        chain_id=envelope["chain_id"],
        validator_address=envelope["validator_address"],
        height=envelope["height"],
        round=envelope["round"],
        vote_type=envelope["vote_type"],
        block_hash=envelope["block_hash"],
    )
    out = dict(envelope)
    out["signature"] = domain.sign_vote(other["private_key"], fields)
    return out


def private_from_seed(seed: bytes) -> Ed25519PrivateKey:
    """Deterministic key for reproducible examples (seed = sha256(label))."""
    return Ed25519PrivateKey.from_private_bytes(seed)
