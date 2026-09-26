"""M-estimator robust kernels applied through Iteratively Reweighted Least Squares.

Each kernel maps the squared Mahalanobis distance ``s = e.T @ Omega @ e`` of an
edge to:

* a scalar cost ``rho(s)`` used for objective evaluation and line search;
* a weight ``w = rho'(s)`` that scales the edge information matrix in the
  Gauss-Newton system.

The linear kernel reproduces ordinary (non-robust) least squares.
"""

import numpy as np

KERNEL_TYPES = ("linear", "huber", "cauchy", "geman_mcclure")


def _check_parameter(kernel_type, parameter):
    if kernel_type != "linear" and (parameter is None or parameter <= 0.0):
        raise ValueError(
            f"kernel {kernel_type!r} requires a positive 'parameter' (kernel width)"
        )


def robust_cost(kernel_type, squared_residual, parameter=None):
    """Robust penalty ``rho(s)`` for squared Mahalanobis residual *s*."""
    s = float(squared_residual)
    if kernel_type == "linear":
        return s
    _check_parameter(kernel_type, parameter)
    k = float(parameter)
    if kernel_type == "huber":
        if s <= k * k:
            return s
        return 2.0 * k * np.sqrt(s) - k * k
    if kernel_type == "cauchy":
        return k * k * np.log1p(s / (k * k))
    if kernel_type == "geman_mcclure":
        return k * k * s / (k * k + s)
    raise ValueError(f"unknown kernel type: {kernel_type!r}")


def robust_weight(kernel_type, squared_residual, parameter=None):
    """IRLS weight ``w = rho'(s)`` in ``(0, 1]`` (linear kernel is always 1)."""
    s = float(squared_residual)
    if kernel_type == "linear":
        return 1.0
    _check_parameter(kernel_type, parameter)
    k = float(parameter)
    if kernel_type == "huber":
        return 1.0 if s <= k * k else k / np.sqrt(s)
    if kernel_type == "cauchy":
        return k * k / (k * k + s)
    if kernel_type == "geman_mcclure":
        return (k * k / (k * k + s)) ** 2
    raise ValueError(f"unknown kernel type: {kernel_type!r}")
