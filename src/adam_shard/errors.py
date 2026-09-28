"""Typed failure categories for checkpoint load/validation.

Every rejection carries a stable ``category`` string so callers (API, logs,
tests) can assert the failure class instead of matching prose.
"""

from __future__ import annotations


class CheckpointError(Exception):
    """Base class for all checkpoint failures."""

    category = "checkpoint_error"

    def __init__(self, message: str, *, commit_id: str | None = None, detail: dict | None = None) -> None:
        super().__init__(message)
        self.commit_id = commit_id
        self.detail = detail or {}


class MissingCommitError(CheckpointError):
    category = "missing_commit"


class MissingShardError(CheckpointError):
    """A shard listed by the manifest is absent on disk."""

    category = "missing_shard"


class IncompleteManifestError(CheckpointError):
    """Manifest listing is incomplete: ranks missing, gaps or count mismatch."""

    category = "incomplete_manifest"


class ShapeMismatchError(CheckpointError):
    """Loaded payload shape/length disagrees with manifest declaration."""

    category = "shape_mismatch"


class DigestMismatchError(CheckpointError):
    """Shard bytes/tensor content disagree with the recorded digest."""

    category = "digest_mismatch"


class CorruptManifestError(CheckpointError):
    """The manifest's own summary digest does not verify."""

    category = "corrupt_manifest"


class CommitMismatchError(CheckpointError):
    """Model and optimizer artifacts do not belong to the same commit."""

    category = "commit_mismatch"


class LayoutMismatchError(CheckpointError):
    """Model layout and optimizer layout describe different parameter sets."""

    category = "layout_mismatch"


class UnsupportedVersionError(CheckpointError):
    category = "unsupported_version"
