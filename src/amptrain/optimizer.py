"""fp32 SGD with momentum, applied to master weights only.

The optimizer never sees low-precision parameters: gradients and weights and
momentum buffers are all fp32.  Updates are functional -- inputs are copied,
never mutated.
"""

from __future__ import annotations

import numpy as np

from .config import MASTER_DTYPE, OptimizerConfig
from .tensors import MasterWeights, OptimizerState, PARAM_NAMES


def sgd_step(
    master: MasterWeights,
    state: OptimizerState,
    gradients: dict[str, np.ndarray],
    cfg: OptimizerConfig,
    lr: float,
) -> tuple[MasterWeights, OptimizerState]:
    """Apply one SGD-momentum step; return new weights and new state."""
    new_momentum: dict[str, np.ndarray] = {}
    new_matrices: dict[str, np.ndarray] = {}
    for name in PARAM_NAMES:
        grad = gradients[name].astype(MASTER_DTYPE)
        if cfg.weight_decay > 0.0:
            grad = grad + np.float32(cfg.weight_decay) * master.matrices[name]
        velocity = (
            np.float32(cfg.momentum) * state.momentum[name] + grad
        ).astype(MASTER_DTYPE)
        new_momentum[name] = velocity
        new_matrices[name] = (
            master.matrices[name] - np.float32(lr) * velocity
        ).astype(MASTER_DTYPE)
    return MasterWeights(matrices=new_matrices), OptimizerState(momentum=new_momentum)
