"""Frequent sequential pattern mining backend service.

Package layout::

    app.config         - configuration layer (env overridable)
    app.errors         - domain error taxonomy
    app.observability  - run-scoped structured logging
    app.models         - pydantic API schemas
    app.corpus         - corpus specification / validation / synthetic fixtures
    app.storage        - SQLite-backed corpus repository
    app.miner          - mining kernel (PrefixSpan projection engine)
    app.service        - orchestration facade over storage + miner
    app.api            - FastAPI HTTP layer with explicit failure codes
"""

__version__ = "1.0.0"
