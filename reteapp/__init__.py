"""Structured Rete production-rule engine.

Layered package layout:

* ``reteapp.lang``     - rule language: model, DSL/JSON parsing, validation, compilation plan
* ``reteapp.core``     - Rete matching network: alpha network, beta memories, agenda, engine
* ``reteapp.storage``  - SQLite evidence store (append-only audit trail)
* ``reteapp.api``      - FastAPI query/command interface
"""

from .version import __version__

__all__ = ["__version__"]
