"""Central finite-difference gradient oracle (independent ground truth).

Perturbs one parameter element at a time while holding the *same fixed
dropout masks*, so the differentiated function is exactly the one the
checkpointed executor must implement.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping

import numpy as np

from .oracle import OracleGraph

DEFAULT_EPS = 1e-6


def finite_difference_param_grads(
    spec: Mapping[str, Any],
    params: Mapping[str, np.ndarray],
    inputs: Mapping[str, np.ndarray],
    masks: Mapping[str, np.ndarray],
    *,
    eps: float = DEFAULT_EPS,
) -> Dict[str, np.ndarray]:
    graph = OracleGraph(spec)
    result: Dict[str, np.ndarray] = {}
    for pid, value in params.items():
        base = np.array(value, dtype=np.float64, copy=True)
        g = np.zeros_like(base)
        it = np.nditer(base, flags=["multi_index"], op_flags=["readonly"])
        while not it.finished:
            idx = it.multi_index
            plus = base.copy()
            minus = base.copy()
            plus[idx] += eps
            minus[idx] -= eps
            p_plus = dict(params)
            p_minus = dict(params)
            p_plus[pid] = plus
            p_minus[pid] = minus
            loss_plus, _, _ = graph.forward(p_plus, inputs, masks)
            loss_minus, _, _ = graph.forward(p_minus, inputs, masks)
            g[idx] = (loss_plus - loss_minus) / (2.0 * eps)
            it.iternext()
        result[pid] = g
    return result
