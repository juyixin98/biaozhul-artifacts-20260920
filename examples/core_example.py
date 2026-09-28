"""Example: use the HVP core directly (no HTTP).

Computes value, gradient and HVP for
    f(x) = 2 x0^2 - 2 x0 x1 + 4 x1^2 + 2 x1 x2 + 6 x2^2
whose Hessian is
        [[ 4, -2,  0],
         [-2,  8,  2],
         [ 0,  2, 12]].
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hvpsvc.state import StateStore

SPEC = {
    "variables": [{"name": "x", "shape": [3]}],
    "expression": {
        "nodes": [
            {"id": "x0", "op": "get", "args": ["x"], "index": 0},
            {"id": "x1", "op": "get", "args": ["x"], "index": 1},
            {"id": "x2", "op": "get", "args": ["x"], "index": 2},
            {"id": "x0sq", "op": "mul", "args": ["x0", "x0"]},
            {"id": "k2", "op": "scalar", "value": 2.0},
            {"id": "q00", "op": "mul", "args": ["k2", "x0sq"]},
            {"id": "x0x1", "op": "mul", "args": ["x0", "x1"]},
            {"id": "km2", "op": "scalar", "value": -2.0},
            {"id": "q01", "op": "mul", "args": ["km2", "x0x1"]},
            {"id": "x1sq", "op": "mul", "args": ["x1", "x1"]},
            {"id": "k4", "op": "scalar", "value": 4.0},
            {"id": "q11", "op": "mul", "args": ["k4", "x1sq"]},
            {"id": "x1x2", "op": "mul", "args": ["x1", "x2"]},
            {"id": "q12", "op": "mul", "args": ["k2", "x1x2"]},
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


def main() -> None:
    store = StateStore()
    state = store.create(SPEC)
    state.set_point({"x": [0.3, -0.5, 0.8]})

    from hvpsvc.autodiff import gradient, hvp

    gg = state.gradient_graph(state.new_budget())
    grad = gradient(gg, state.point, state.layout, state.new_budget())
    v = np.array([1.0, -2.0, 3.0])
    product = hvp(gg, state.point, v, state.layout,
                  state.new_budget(), state.nonsmooth)

    hessian = np.array([[4.0, -2.0, 0.0],
                        [-2.0, 8.0, 2.0],
                        [0.0, 2.0, 12.0]])
    print("value      :", gg.primal_value)
    print("gradient   :", grad)
    print("H @ v (AD) :", product)
    print("H @ v (ref):", hessian @ v)
    assert np.allclose(product, hessian @ v)


if __name__ == "__main__":
    main()
