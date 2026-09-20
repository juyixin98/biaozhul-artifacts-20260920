import hashlib
import hmac


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def subject_pseudonym(organization_id: int, subject_ref: str) -> str:
    """One-way pseudonym retained in immutable history after erasure."""
    return sha256_hex(f"subject:{organization_id}:{subject_ref}")


def api_key_hash(raw_key: str) -> str:
    return sha256_hex(f"apikey:{raw_key}")


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a, b)
