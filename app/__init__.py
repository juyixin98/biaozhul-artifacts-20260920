"""Real-root isolation service package.

Importing the package raises the Python integer-to-string digit cap to a
value consistent with the kernel's explicit coefficient bit budget and
bisection depth (both configured in ``config/default.yaml``). The default
PEP 682 cap of 4300 digits would otherwise reject legitimate exact rationals
produced during isolation; bounding it here keeps the surface explicit
rather than silently inherited.
"""
from __future__ import annotations

import sys

# Keep above max_coeff_bits (20000) plus bisection denominator growth
# (~6000 digits at the 20000-halving budget), with headroom for diagnostics.
_INTEGER_STRING_DIGITS = 50000

if hasattr(sys, "set_int_max_str_digits"):
    sys.set_int_max_str_digits(_INTEGER_STRING_DIGITS)

__version__ = "1.0.0"
