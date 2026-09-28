"""Complex polynomial root-finding backend.

Package layout (engineering boundaries):
- app.errors      : error taxonomy shared across all layers
- app.domain      : data contracts between layers
- app.validation  : numeric input boundary (parse / normalize / reject)
- app.kernel      : compute kernels (companion matrix, Aberth iteration)
- app.evidence    : error evidence (residuals, reconstruction, Vieta, clusters)
- app.ordering    : deterministic root ordering + conjugate pairing
- app.runlog      : structured run logging for replay
- app.service     : orchestration, run registry, status decisions
- app.api         : FastAPI HTTP interface
"""

__version__ = "0.1.0"
