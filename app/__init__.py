"""Bracket structure index service.

Module map
----------
- ``app.corpus``     : corpus/lexicon specification (bracket types, quote and
  escape rules) and deterministic synthetic corpus generation.
- ``app.mining``     : the mining kernel -- quote-aware lexer, chunk reduction
  that keeps type ORDER (not only net counts), a persistent rope/treap index
  with path-local invalidation, and the edit/query facade.
- ``app.storage``    : SQLite connection / schema and the repository.
- ``app.models``     : pydantic API schemas.
- ``app.validation`` : query validation, defect classification, diagnostic
  assembly (accept / reject / undetermined with request ids).
- ``app.api``        : FastAPI routes and envelope wiring.
"""

__version__ = "0.1.0"
