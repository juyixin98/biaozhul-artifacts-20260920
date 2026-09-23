"""Custom exception mapped to HTTP 422 by the API."""


class InvalidInputError(ValueError):
    """Raised for malformed measurement data (as opposed to schema errors)."""
