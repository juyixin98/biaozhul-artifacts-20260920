"""Logging setup with output-layer secret redaction.

Defense in depth: application code should not log secrets, but every record
also passes through ``RedactingFormatter`` which masks Authorization headers,
Signature parameters, and the literal secret values loaded into the process.
The formatter never reveals what it removed beyond the word 'REDACTED'.
"""

from __future__ import annotations

import logging
import re

_SIGNATURE_RE = re.compile(r"((?i:signature)\s*[=:]\s*)([0-9a-fA-F]{16,64})")
_AUTH_HEADER_RE = re.compile(r"((?i:authorization)\s*[=:]\s*)(?:'|\")?[^\s,'\"]+")
_BEARER_LIKE_RE = re.compile(r"(HMAC-SHA256\s+Credential=)\S+")


class RedactingFormatter(logging.Formatter):
    def __init__(self, *args, secrets: list[str] | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        # Longest first so substrings never leak after a shorter match.
        self._secrets = sorted(
            {s for s in (secrets or []) if s}, key=len, reverse=True
        )

    def redact(self, text: str) -> str:
        text = _AUTH_HEADER_RE.sub(r"\1REDACTED", text)
        text = _BEARER_LIKE_RE.sub(r"\1REDACTED", text)
        text = _SIGNATURE_RE.sub(r"\1REDACTED", text)
        for secret in self._secrets:
            text = text.replace(secret, "REDACTED")
        return text

    def format(self, record: logging.LogRecord) -> str:  # noqa: A003
        rendered = super().format(record)
        return self.redact(rendered)


def configure_logging(secrets: list[str] | None = None, level: int = logging.INFO) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(
        RedactingFormatter("%(asctime)s %(levelname)s %(name)s %(message)s", secrets=secrets)
    )
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
