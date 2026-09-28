"""Association rule lift audit backend.

Modules:
    config       - environment driven configuration
    models       - framework-neutral domain dataclasses and status codes
    diagnostics  - structured, redacted audit records with request ids
    corpus       - corpus specification and normalization (dedup semantics)
    indices      - transaction index and frequent-itemset support table
    mining       - metric kernel (confidence/lift/leverage) and rule enumeration
    apriori      - level-wise Apriori used to *materialize* frequent itemsets
    validation   - rule query validation and range rejection
    repository   - SQLite persistence
    service      - orchestration / audit pipeline
    schemas      - FastAPI/pydantic DTOs
    main         - FastAPI application factory and routes
"""

__version__ = "1.0.0"
