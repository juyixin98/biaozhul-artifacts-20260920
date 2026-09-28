"""Typed error categories for checkpoint failures.

Tests and the HTTP layer assert on the *category*, never on a message string,
so failure modes stay specific and contract-level.
"""
from __future__ import annotations


class CheckpointError(Exception):
    """Base class for all checkpoint failures."""

    category = "checkpoint_error"

    def __init__(self, message: str, *, request_id: str | None = None) -> None:
        super().__init__(message)
        self.request_id = request_id

    def to_dict(self) -> dict:
        return {
            "ok": False,
            "category": self.category,
            "error": str(self),
            "request_id": self.request_id,
        }


class MissingShardError(CheckpointError):
    """A shard listed in the manifest is absent or unreadable."""

    category = "missing_shard"


class IncompleteManifestError(CheckpointError):
    """Manifest does not cover the full flat parameter range."""

    category = "incomplete_manifest"


class ShapeMismatchError(CheckpointError):
    """Stored tensor shape disagrees with declared parameter identity."""

    category = "shape_mismatch"


class DigestMismatchError(CheckpointError):
    """A shard payload/summary fails its content hash check."""

    category = "digest_mismatch"


class CommitMismatchError(CheckpointError):
    """Model and optimizer checkpoint parts were not committed together."""

    category = "commit_mismatch"


class ParameterUnknownError(CheckpointError):
    """Restore graph lacks a parameter named in the checkpoint."""

    category = "parameter_unknown"


class CorruptManifestError(CheckpointError):
    """Manifest JSON is unparseable or structurally invalid."""

    category = "corrupt_manifest"


CATEGORY_TO_EXC: dict[str, type[CheckpointError]] = {
    cls.category: cls
    for cls in (
        MissingShardError,
        IncompleteManifestError,
        ShapeMismatchError,
        DigestMismatchError,
        CommitMismatchError,
        ParameterUnknownError,
        CorruptManifestError,
    )
}
