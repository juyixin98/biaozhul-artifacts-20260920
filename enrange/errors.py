"""Typical error hierarchy for the enrange store.

Anything that prevents authenticating stored bytes (bad AEAD tag, structural
truncation, a header that does not authenticate) raises AuthenticationError:
the safe response is to never return plaintext derived from those bytes.
"""


class StoreError(Exception):
    """Base class for all store errors."""


class NotFoundError(StoreError):
    """No object with the given id exists in the store."""


class AuthenticationError(StoreError):
    """Stored bytes failed authentication (or were structurally invalid).

    Raised for AEAD tag failures and for truncation/structural corruption,
    both of which are indistinguishable from tampering and handled the same
    way: no plaintext is returned.
    """


class InvalidRangeError(StoreError):
    """Requested byte range is malformed or unsatisfiable."""
