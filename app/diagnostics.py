"""Diagnostics helpers: redaction of sensitive symbol names.

When ``log_symbol_names`` is disabled (the default), assumption and node
names appear in diagnostic records only as short hashes, so a session using
sensitive vocabulary never leaks it into logs. Counts, statuses and reasons
are always logged in the clear -- they carry no sensitive content.
"""

from __future__ import annotations

import hashlib


def redact_name(name: str, log_names: bool) -> str:
    if log_names:
        return name
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:10]
    return f"sym:{digest}"


def redact_detail(detail: dict, log_names: bool) -> dict:
    """Redact the well-known name-carrying fields of a diagnostic detail."""
    redacted = {}
    for key, value in detail.items():
        if key in {"name", "node", "consequent"} and isinstance(value, str):
            redacted[key] = redact_name(value, log_names)
        else:
            redacted[key] = value
    return redacted
