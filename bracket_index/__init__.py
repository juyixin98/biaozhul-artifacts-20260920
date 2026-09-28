"""Typed-bracket structure indexing over large text.

Modules:
- corpus:   lexical specification (bracket types, quotes, escapes) and synthetic fixtures
- kernel:   mining kernel — lexer, chunk summaries, composition, full-scan oracle
- index:    SQLite-backed chunked index with versioning and local-edit invalidation
- api:      FastAPI query/validation layer
"""

__version__ = "0.1.0"
