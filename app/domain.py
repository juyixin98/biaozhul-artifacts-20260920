"""Domain rules: real cryptography, canonical encoding and evidence identity.

Everything here is deterministic and has no database dependency so it can be
unit-tested in isolation.

Protocol (judgment) version v1
------------------------------
* Every offline vote is an Ed25519 signature over a canonical message that
  binds the vote to a specific chain (``chain_id`` is part of the signed body).
  Replaying a vote on a different chain therefore fails signature verification
  — cross-chain vote mixing is rejected cryptographically, not by convention.
* Validator address = lower-hex(SHA-256(public_key)[0:20]).
* Two votes are a double sign iff they share
  (chain_id, validator_address, height, round, vote_type) but carry different
  block_hash values. Identical votes (same block_hash) are duplicates, not a
  double sign; prevote vs precommit are different messages.
* Evidence identity is order-independent: the two vote records are canonicalized
  to JSON, byte-sorted and hashed, so reverse arrival order yields the same ID.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

VOTE_SIGNING_DOMAIN = "EPE-OFFLINE-VOTE/v1"
EVIDENCE_DOMAIN = "EPE-EVIDENCE/v1"

VOTE_TYPES = ("prevote", "precommit")
ADDRESS_HEX_LEN = 40  # 20 bytes
BLOCK_HASH_HEX_LEN = 64  # 32 bytes
PUBKEY_HEX_LEN = 64  # 32 bytes
SIGNATURE_HEX_LEN = 128  # 64 bytes


class DomainError(ValueError):
    """Validation failure whose reason is safe to return to the API client."""


def derive_address(public_key: bytes) -> str:
    """Derive the on-chain validator address from an Ed25519 public key."""
    return hashlib.sha256(public_key).digest()[:20].hex()


def canonical_json(obj: dict) -> bytes:
    """Stable JSON encoding: sorted keys, no whitespace, UTF-8."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


@dataclass(frozen=True)
class VoteFields:
    """The authenticated body of an offline vote."""

    chain_id: str
    validator_address: str
    height: int
    round: int
    vote_type: str
    block_hash: str

    def to_signed_dict(self) -> dict:
        return {
            "chain_id": self.chain_id,
            "validator_address": self.validator_address,
            "height": self.height,
            "round": self.round,
            "vote_type": self.vote_type,
            "block_hash": self.block_hash,
        }


def signing_message(fields: VoteFields) -> bytes:
    """Build the exact byte string the validator signs.

    The chain_id is inside the signed body, so a signature produced for chain A
    is invalid for chain B.
    """
    return VOTE_SIGNING_DOMAIN.encode("ascii") + b"\n" + canonical_json(fields.to_signed_dict())


def sign_vote(private_key: Ed25519PrivateKey, fields: VoteFields) -> str:
    """Sign a vote with an Ed25519 private key; return lower-hex signature."""
    return private_key.sign(signing_message(fields)).hex()


def parse_public_key(public_key_hex: str) -> bytes:
    _require_hex(public_key_hex, PUBKEY_HEX_LEN, "public_key")
    return bytes.fromhex(public_key_hex)


def parse_signature(signature_hex: str) -> bytes:
    _require_hex(signature_hex, SIGNATURE_HEX_LEN, "signature")
    return bytes.fromhex(signature_hex)


def validate_envelope(envelope: dict) -> tuple[VoteFields, bytes, bytes]:
    """Validate the wire envelope and return (fields, public_key, signature).

    Raises DomainError on any malformed field. Does not touch the database and
    does not verify the signature (see verify_signature).
    """
    required = {
        "chain_id",
        "validator_address",
        "height",
        "round",
        "vote_type",
        "block_hash",
        "public_key",
        "signature",
    }
    missing = required - envelope.keys()
    if missing:
        raise DomainError(f"missing fields: {sorted(missing)}")

    chain_id = envelope["chain_id"]
    if not isinstance(chain_id, str) or not chain_id or len(chain_id) > 128:
        raise DomainError("chain_id must be a non-empty string (<=128 chars)")

    addr = envelope["validator_address"]
    _require_hex(addr, ADDRESS_HEX_LEN, "validator_address")

    for name in ("height", "round"):
        value = envelope[name]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > 2**63 - 1:
            raise DomainError(f"{name} must be a non-negative 64-bit integer")

    vote_type = envelope["vote_type"]
    if vote_type not in VOTE_TYPES:
        raise DomainError(f"vote_type must be one of {list(VOTE_TYPES)}")

    _require_hex(envelope["block_hash"], BLOCK_HASH_HEX_LEN, "block_hash")

    public_key = parse_public_key(envelope["public_key"])
    signature = parse_signature(envelope["signature"])

    derived = derive_address(public_key)
    if derived != addr:
        raise DomainError(
            f"validator_address does not match public_key (derived {derived})"
        )

    return (
        VoteFields(
            chain_id=chain_id,
            validator_address=addr,
            height=envelope["height"],
            round=envelope["round"],
            vote_type=vote_type,
            block_hash=envelope["block_hash"].lower(),
        ),
        public_key,
        signature,
    )


def verify_signature(fields: VoteFields, public_key: bytes, signature: bytes) -> None:
    """Verify a real Ed25519 signature. Raises DomainError if invalid."""
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature, signing_message(fields))
    except InvalidSignature:
        raise DomainError("signature verification failed")


def canonical_vote_record(fields: VoteFields, public_key: bytes, signature: bytes) -> bytes:
    """Canonical byte form of one signed vote as stored inside evidence."""
    record = {
        "chain_id": fields.chain_id,
        "validator_address": fields.validator_address,
        "height": fields.height,
        "round": fields.round,
        "vote_type": fields.vote_type,
        "block_hash": fields.block_hash,
        "public_key": public_key.hex(),
        "signature": signature.hex(),
    }
    return canonical_json(record)


def evidence_identity(vote_records: list[bytes]) -> tuple[str, bytes]:
    """Compute (evidence_id, canonical_bytes) from the conflicting vote records.

    Order-independent: records are byte-sorted before hashing, so votes arriving
    in reverse order produce the identical id and body.
    """
    if len(vote_records) < 2:
        raise ValueError("evidence requires at least two conflicting votes")
    ordered = sorted(vote_records)
    body = EVIDENCE_DOMAIN.encode("ascii") + b"\n" + b"\n".join(ordered)
    return hashlib.sha256(body).hexdigest(), body


def epoch_of_round(round_index: int, rounds_per_epoch: int) -> int:
    """Epoch number for a round (both 0-based, contiguous)."""
    return round_index // rounds_per_epoch


def slash_power(frozen_power: int, slash_rate_ppm: int) -> int:
    """Integer slashing math: floor(frozen * ppm / 1_000_000)."""
    return frozen_power * slash_rate_ppm // 1_000_000


def generate_private_key() -> Ed25519PrivateKey:
    """Test helper: generate a real Ed25519 keypair."""
    return Ed25519PrivateKey.generate()


def public_hex(private_key: Ed25519PrivateKey) -> str:
    return private_key.public_key().public_bytes_raw().hex()


def _require_hex(value: object, expected_len: int, name: str) -> None:
    if not isinstance(value, str) or len(value) != expected_len:
        raise DomainError(f"{name} must be {expected_len} hex chars")
    try:
        bytes.fromhex(value)
    except ValueError:
        raise DomainError(f"{name} must be valid hexadecimal")
