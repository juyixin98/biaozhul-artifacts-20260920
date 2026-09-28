"""stratblock: stratified permuted-block random allocation service.

Modules
-------
config         frozen configuration object (single source of config)
contract       statistical contract: study configuration, validity rules, enums
rng            seeded stream derivation / block random sequence kernel
storage        SQLite-backed allocation repository
estimator      estimation kernel (difference in means, Welch, permutation)
diagnostics    evidence + diagnostics (balance tables, stream provenance)
replay         reproducible experiment replay from audit events
api            FastAPI application, allocation concealment / audit isolation
"""
__version__ = "1.0.0"
