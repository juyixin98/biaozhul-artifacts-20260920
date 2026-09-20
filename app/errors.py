"""Domain error mapped to a structured HTTP response.

Every business-rule failure raises DomainError with a stable machine-readable
``code`` so API clients can branch on it instead of parsing message text.
"""
from __future__ import annotations


class DomainError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details
