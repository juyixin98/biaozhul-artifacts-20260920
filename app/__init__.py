"""2SLS service package.

Modules have real, separated responsibilities:

- ``contracts``      statistical contract: request/response schemas, assumptions, verdicts
- ``kernel``         estimation core: first stage, 2SLS, VCV, GMM, Sargan/Hausman
- ``diagnostics``    evidence & diagnostics: rank, weak instruments, VIF, decision record
- ``dgp``            reproducible synthetic data generation (fixtures / experiments)
- ``storage``        SQLite persistence of requests and decision records
- ``api``            FastAPI service boundary (validation, errors, redacted logging)
"""

__version__ = "0.1.0"
