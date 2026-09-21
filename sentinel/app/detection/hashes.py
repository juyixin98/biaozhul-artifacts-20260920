from __future__ import annotations

import hashlib
import json
from typing import Any

# Content over which same-(device,event_id) retries must agree.
# occurred_at is rendered as an ISO-8601 string preserving its offset.
_HASH_FIELDS = ("type", "occurred_at", "payload")


def canonical_event_content(event_type: str, occurred_at_iso: str, payload: dict[str, Any]) -> str:
    body = {
        "type": event_type,
        "occurred_at": occurred_at_iso,
        "payload": payload or {},
    }
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def content_hash(event_type: str, occurred_at: Any, payload: dict[str, Any]) -> str:
    iso = occurred_at.isoformat() if hasattr(occurred_at, "isoformat") else str(occurred_at)
    canonical = canonical_event_content(event_type, iso, payload)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
