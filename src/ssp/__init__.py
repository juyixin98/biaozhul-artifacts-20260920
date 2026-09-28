"""Sample size planner backend.

Package layout::

    ssp.config         - environment-driven configuration
    ssp.diagnostics    - run identity, structured logging
    ssp.contracts      - statistical contract types (enums, request/result models)
    ssp.errors         - typed failure categories
    ssp.kernels        - estimation kernels (normal / binomial, exact + asymptotic)
    ssp.planning       - integer search orchestration, method switching
    ssp.evidence       - Monte-Carlo simulated-power evidence
    ssp.repro          - reproducibility (RNG seeds, experiment fixtures)
    ssp.storage        - SQLite persistence
    ssp.api            - FastAPI application and routers
"""

__version__ = "0.1.0"
