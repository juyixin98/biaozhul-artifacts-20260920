"""enrange: fixed-block AEAD object storage with authenticated range reads."""

from .errors import (
    AuthenticationError,
    InvalidRangeError,
    NotFoundError,
    StoreError,
)
from .store import ObjectStore

__all__ = [
    "ObjectStore",
    "StoreError",
    "NotFoundError",
    "AuthenticationError",
    "InvalidRangeError",
]
__version__ = "0.1.0"
