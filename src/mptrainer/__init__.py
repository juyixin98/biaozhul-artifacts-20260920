"""mptrainer: mixed-precision trainer for a synthetic small network.

Layers of the package:
- tensors:     dtype handling, casting, finite checks (tensor types)
- graph:       forward/backward of the MLP (compute graph)
- optimizer:   SGD+momentum over fp32 master weights (training state)
- scaler:      dynamic loss scaling state machine (training state)
- trainer:     step semantics, gradient accumulation, overflow policy
- checkpoint:  persistence of the full training state incl. scaler
- validation:  numerical comparison helpers used by tests and demo
- runlog:      structured JSONL run logging correlated by run_id
"""

from .version import __version__

__all__ = ["__version__"]
