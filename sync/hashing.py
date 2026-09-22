import hashlib
import json


def canonical_content_hash(data: dict) -> str:
    """Stable SHA-256 over submitted field values.

    Keys are sorted and separators compacted so the same logical content
    always hashes identically regardless of client key order or whitespace.
    Collection metadata (collected_at, base version) is intentionally
    excluded: re-submitting identical answers is an idempotent retry even if
    the device clock shifted slightly.
    """
    payload = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
