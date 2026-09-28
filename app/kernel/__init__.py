"""Compute kernels: companion-matrix eigensolver and Aberth simultaneous iteration.

Kernels receive a NormalizedPolynomial (descending-power coefficients) and
return raw root arrays plus convergence state. They never decide HTTP semantics;
numeric failures raise ComputationFailedError.
"""

from .companion import companion_roots
from .aberth import aberth_refine, cauchy_initial_guesses, AberthResult

__all__ = ["companion_roots", "aberth_refine", "cauchy_initial_guesses", "AberthResult"]
