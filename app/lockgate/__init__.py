"""Dependency-lock consistency gate (offline Node lockfile auditor)."""
from .audit import audit
from .platforms import Target

__all__ = ["audit", "Target"]
__version__ = "1.0.0"
