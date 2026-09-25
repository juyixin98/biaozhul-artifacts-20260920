"""Offline X.509 certificate chain verification toolkit.

This package never opens a network socket and never checks revocation.
Trust anchors, verification time, key purpose and hostname are always
supplied explicitly by the caller.
"""

from .models import Purpose, VerificationOptions, VerificationResult
from .verify import verify_chain

__all__ = ["Purpose", "VerificationOptions", "VerificationResult", "verify_chain"]
__version__ = "1.0.0"
