"""Synthetic local fixtures: expression specs + analytic answers.

Analytic answers here are hand-derived constants, NOT produced by the
autodiff core. The mpmath reference (hvpsvc.validation) is an independent
interpreter used in addition to these constants.
"""

from __future__ import annotations

import numpy as np


def quadratic3_spec() -> dict:
    """f = 2 x0^2 - 2 x0 x1 + 4 x1^2 + 2 x1 x2 + 6 x2^2.

    Hessian = [[4,-2,0],[-2,8,2],[0,2,12]].
    """
    return {
        "variables": [{"name": "x", "shape": [3]}],
        "expression": {
            "nodes": [
                {"id": "x0", "op": "get", "args": ["x"], "index": 0},
                {"id": "x1", "op": "get", "args": ["x"], "index": 1},
                {"id": "x2", "op": "get", "args": ["x"], "index": 2},
                # 2 x0^2
                {"id": "x0sq", "op": "mul", "args": ["x0", "x0"]},
                {"id": "k2", "op": "scalar", "value": 2.0},
                {"id": "q00", "op": "mul", "args": ["k2", "x0sq"]},
                # -2 x0 x1
                {"id": "x0x1", "op": "mul", "args": ["x0", "x1"]},
                {"id": "km2", "op": "scalar", "value": -2.0},
                {"id": "q01", "op": "mul", "args": ["km2", "x0x1"]},
                # 4 x1^2
                {"id": "x1sq", "op": "mul", "args": ["x1", "x1"]},
                {"id": "k4", "op": "scalar", "value": 4.0},
                {"id": "q11", "op": "mul", "args": ["k4", "x1sq"]},
                # 2 x1 x2
                {"id": "x1x2", "op": "mul", "args": ["x1", "x2"]},
                {"id": "q12", "op": "mul", "args": ["k2", "x1x2"]},
                # 6 x2^2
                {"id": "x2sq", "op": "mul", "args": ["x2", "x2"]},
                {"id": "k6", "op": "scalar", "value": 6.0},
                {"id": "q22", "op": "mul", "args": ["k6", "x2sq"]},
                {"id": "s01", "op": "add", "args": ["q00", "q01"]},
                {"id": "s012", "op": "add", "args": ["s01", "q11"]},
                {"id": "s0123", "op": "add", "args": ["s012", "q12"]},
                {"id": "f", "op": "add", "args": ["s0123", "q22"]},
            ],
            "output": "f",
        },
    }


H_QUAD3 = np.array([[4.0, -2.0, 0.0],
                    [-2.0, 8.0, 2.0],
                    [0.0, 2.0, 12.0]])
X_QUAD3 = np.array([0.3, -0.5, 0.8])
V_QUAD3 = np.array([1.0, -2.0, 3.0])


def scalar_square_spec() -> dict:
    """g(s) = s^2 + s with a *scalar* variable. H = 2 (1x1)."""
    return {
        "variables": [{"name": "s", "shape": []}],
        "expression": {
            "nodes": [
                {"id": "ssq", "op": "mul", "args": ["s", "s"]},
                {"id": "g", "op": "add", "args": ["ssq", "s"]},
            ],
            "output": "g",
        },
    }


def shared_subgraph_spec() -> dict:
    """f = y^2 + y, y = x0*x1 (node y consumed twice, built once).

    H = [[2 x1^2, 4 x0 x1 + 1], [4 x0 x1 + 1, 2 x0^2]].
    At x0=1.5, x1=-0.4: H = [[0.32, -1.4], [-1.4, 4.5]].
    """
    return {
        "variables": [{"name": "x", "shape": [2]}],
        "expression": {
            "nodes": [
                {"id": "x0", "op": "get", "args": ["x"], "index": 0},
                {"id": "x1", "op": "get", "args": ["x"], "index": 1},
                {"id": "y", "op": "mul", "args": ["x0", "x1"]},
                {"id": "y2", "op": "mul", "args": ["y", "y"]},
                {"id": "f", "op": "add", "args": ["y2", "y"]},
            ],
            "output": "f",
        },
    }


H_SHARED = np.array([[0.32, -1.4], [-1.4, 4.5]])
X_SHARED = np.array([1.5, -0.4])


def nonlinear_spec() -> dict:
    """f = sin(x0)*x1 + exp(x2) + log(x1^2 + 2) + sqrt(x0^2 + 1).

    Smooth, mixes all smooth unary/binary rules; verified numerically.
    """
    return {
        "variables": [{"name": "x", "shape": [3]}],
        "expression": {
            "nodes": [
                {"id": "x0", "op": "get", "args": ["x"], "index": 0},
                {"id": "x1", "op": "get", "args": ["x"], "index": 1},
                {"id": "x2", "op": "get", "args": ["x"], "index": 2},
                {"id": "sx", "op": "sin", "args": ["x0"]},
                {"id": "cross", "op": "mul", "args": ["sx", "x1"]},
                {"id": "ex", "op": "exp", "args": ["x2"]},
                {"id": "x1sq", "op": "mul", "args": ["x1", "x1"]},
                {"id": "two", "op": "scalar", "value": 2.0},
                {"id": "arg", "op": "add", "args": ["x1sq", "two"]},
                {"id": "lg", "op": "log", "args": ["arg"]},
                {"id": "x0sq", "op": "mul", "args": ["x0", "x0"]},
                {"id": "one", "op": "scalar", "value": 1.0},
                {"id": "arg2", "op": "add", "args": ["x0sq", "one"]},
                {"id": "sq", "op": "sqrt", "args": ["arg2"]},
                {"id": "a", "op": "add", "args": ["cross", "ex"]},
                {"id": "b", "op": "add", "args": ["a", "lg"]},
                {"id": "f", "op": "add", "args": ["b", "sq"]},
            ],
            "output": "f",
        },
    }


X_NONLINEAR = np.array([0.7, -0.3, 0.2])


def nonsmooth_spec() -> dict:
    """f = |x0| + relu(x1) + max(x0, x1)."""
    return {
        "variables": [{"name": "z", "shape": [2]}],
        "expression": {
            "nodes": [
                {"id": "z0", "op": "get", "args": ["z"], "index": 0},
                {"id": "z1", "op": "get", "args": ["z"], "index": 1},
                {"id": "a", "op": "abs", "args": ["z0"]},
                {"id": "r", "op": "relu", "args": ["z1"]},
                {"id": "m", "op": "max", "args": ["z0", "z1"]},
                {"id": "t", "op": "add", "args": ["a", "r"]},
                {"id": "f", "op": "add", "args": ["t", "m"]},
            ],
            "output": "f",
        },
    }


def log_domain_spec() -> dict:
    return {
        "variables": [{"name": "x", "shape": []}],
        "expression": {
            "nodes": [
                {"id": "lg", "op": "log", "args": ["x"]},
            ],
            "output": "lg",
        },
    }


def exp_spec() -> dict:
    return {
        "variables": [{"name": "x", "shape": []}],
        "expression": {
            "nodes": [
                {"id": "ex", "op": "exp", "args": ["x"]},
            ],
            "output": "ex",
        },
    }


def two_variable_spec() -> dict:
    """f = (x[0]-y)^2 + y*x[0]: parameters shared across two variables."""
    return {
        "variables": [{"name": "x", "shape": [1]}, {"name": "y", "shape": []}],
        "expression": {
            "nodes": [
                {"id": "x0", "op": "get", "args": ["x"], "index": 0},
                {"id": "d", "op": "sub", "args": ["x0", "y"]},
                {"id": "d2", "op": "mul", "args": ["d", "d"]},
                {"id": "yx", "op": "mul", "args": ["y", "x0"]},
                {"id": "f", "op": "add", "args": ["d2", "yx"]},
            ],
            "output": "f",
        },
    }
