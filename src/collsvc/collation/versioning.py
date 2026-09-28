"""Index version derivation.

An :class:`IndexVersion` pins down *everything* that determines the stored
sort keys: the rules fingerprint (library/Unicode version, locale tailoring,
strength, numeric, case-first) and the schema generation. Two option sets are
compatible iff their versions are byte-identical; anything else requires a
rebuild and invalidates old cursors.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from .engine import CollationEngine
from .options import CollationOptions

# Bump when the on-disk schema / key encoding changes.
SCHEMA_GENERATION = 1

_VERSION_RE = re.compile(r"^idx_v\d+_[0-9a-f]{16}$")


@dataclass(frozen=True)
class IndexVersion:
    schema_generation: int
    rules_fingerprint: str
    version_id: str

    @staticmethod
    def derive(rules_fingerprint: str,
               schema_generation: int = SCHEMA_GENERATION) -> "IndexVersion":
        digest = hashlib.sha256(
            json.dumps(
                {
                    "schema_generation": schema_generation,
                    "rules_fingerprint": rules_fingerprint,
                },
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()[:16]
        return IndexVersion(
            schema_generation=schema_generation,
            rules_fingerprint=rules_fingerprint,
            version_id=f"idx_v{schema_generation}_{digest}",
        )

    @staticmethod
    def for_engine(engine: CollationEngine) -> "IndexVersion":
        return IndexVersion.derive(engine.rules_fingerprint())

    @staticmethod
    def parse(token: str) -> "IndexVersion":
        if not isinstance(token, str) or not _VERSION_RE.match(token):
            raise InvalidCursor(f"unparseable index version token: {token!r}")
        generation = int(token.split("_")[1].lstrip("v"))
        return IndexVersion(
            schema_generation=generation,
            rules_fingerprint="",
            version_id=token,
        )

    def __str__(self) -> str:
        return self.version_id

    def as_token(self) -> str:
        return self.version_id


class VersionMismatchError(ValueError):
    """A query/cursor was built for a different index version."""


class InvalidCursor(ValueError):
    """A continuation token is malformed or for an unknown version."""
