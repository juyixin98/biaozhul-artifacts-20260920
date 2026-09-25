"""Shared error types for the feature pipeline package."""


class NotFittedError(RuntimeError):
    """Raised when transform/predict is called before fit."""
