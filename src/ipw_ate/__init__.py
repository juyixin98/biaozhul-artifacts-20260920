"""IPW-ATE: cross-fitted inverse-probability-weighted ATE with overlap diagnostics.

Module layout
-------------
- :mod:`ipw_ate.contract`  statistical contract: data records, config, decision codes
- :mod:`ipw_ate.errors`    explicit, classified failure types
- :mod:`ipw_ate.propensity` propensity model + declared cross-fitting
- :mod:`ipw_ate.weights`   stable IPW weights, fixed truncation, ESS
- :mod:`ipw_ate.estimator` ATE/ATT point estimates, asymptotic + influence-function SE
- :mod:`ipw_ate.diagnostics` overlap evidence and accept/reject/inconclusive rules
- :mod:`ipw_ate.pipeline`  end-to-end orchestration
- :mod:`ipw_ate.io_csv`    CSV boundary loading/validation
- :mod:`ipw_ate.api`       FastAPI service
"""

__version__ = "1.0.0"
