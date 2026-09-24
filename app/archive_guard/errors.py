"""Typed exceptions raised during archive inspection / extraction.

All exceptions carry a stable ``code`` string so the HTTP layer can map
failures to status codes without parsing free-text messages.
"""


class ArchiveGuardError(Exception):
    """Base class for all archive guard failures."""

    code = "archive_guard_error"
    #: HTTP status the API layer should return for this failure.
    http_status = 400

    def __init__(self, message: str, *, member: str | None = None):
        super().__init__(message)
        self.message = message
        #: Name of the offending archive member, when known.
        self.member = member

    def to_dict(self) -> dict:
        d = {"error": self.code, "message": self.message}
        if self.member is not None:
            d["member"] = self.member
        return d


class InvalidArchiveError(ArchiveGuardError):
    """The upload is not a readable tar archive (or truncated)."""

    code = "invalid_archive"
    http_status = 422


class UnsafeArchiveError(ArchiveGuardError):
    """The archive passes structural parsing but violates a safety policy."""

    code = "unsafe_archive"
    http_status = 422


class QuotaExceededError(ArchiveGuardError):
    """Writing the archive would exceed a configured byte/entry quota."""

    code = "quota_exceeded"
    http_status = 413

    def __init__(self, message: str, *, member: str | None = None,
                 limit: int | None = None, actual: int | None = None):
        super().__init__(message, member=member)
        self.limit = limit
        self.actual = actual

    def to_dict(self) -> dict:
        d = super().to_dict()
        if self.limit is not None:
            d["limit"] = self.limit
        if self.actual is not None:
            d["actual"] = self.actual
        return d
