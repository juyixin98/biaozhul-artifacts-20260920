"""Typed error categories for the Toeplitz FFT backend.

Every failure raised by the kernel or the service layer carries a stable
``category`` string so tests and API clients can assert on the failure
*kind*, not just on "an exception happened".
"""

from __future__ import annotations


class ToeplitzError(Exception):
    """Base class for all backend errors."""

    category = "toeplitz_error"


class InconsistentToeplitzError(ToeplitzError):
    """First column c and first row r disagree on the shared element c[0] == r[0]."""

    category = "inconsistent_shared_element"


class ShapeMismatchError(ToeplitzError):
    """Vector/matrix dimensions do not match the Toeplitz operator dimensions."""

    category = "shape_mismatch"


class UnsupportedModeError(ToeplitzError):
    """Requested real/complex mode is incompatible with the input data."""

    category = "unsupported_mode"


class ConfigurationError(ToeplitzError):
    """Invalid configuration values."""

    category = "configuration_error"
