"""Archive guard: safe preflight inspection and quarantined tar extraction."""

from .errors import (
    InvalidArchiveError,
    QuotaExceededError,
    UnsafeArchiveError,
)
from .limits import Limits
from .safety import (
    MemberRecord,
    PreflightReport,
    inspect_tar,
    safe_extract,
)

__all__ = [
    "InvalidArchiveError",
    "QuotaExceededError",
    "UnsafeArchiveError",
    "Limits",
    "MemberRecord",
    "PreflightReport",
    "inspect_tar",
    "safe_extract",
]
