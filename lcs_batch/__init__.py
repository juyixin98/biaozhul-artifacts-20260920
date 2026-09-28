"""lcs_batch: multi-document longest common substring batch retrieval.

Modules:
    corpus       - corpus specification and separator-safe encoding
    kernel       - generalized suffix array + LCP mining kernel
    index_store  - SQLite persistence for indexes, models and query audit log
    validation   - query/corpus validation with typed failure categories
    service      - orchestration: build, batch query, explainable results
    api          - FastAPI HTTP surface
"""

__version__ = "0.1.0"
