"""Persistence layer (SQLite).

The index stores three kinds of materialised models per corpus:

* validated corpus specification (raw JSON);
* every transducer as an explicit FST document (lexicon entries are
  expanded at load time, never at query time);
* every pipeline as a precomposed FST document.

Query runs, ranked results and structured log lines are kept too, so a
result can be audited after the fact and correlated with its run id.
"""

from .db import connect, init_schema, SCHEMA_VERSION
from .repository import IndexRepository, StoredRun
from .service import IndexService, LoadReport

__all__ = [
    "connect",
    "init_schema",
    "SCHEMA_VERSION",
    "IndexRepository",
    "StoredRun",
    "IndexService",
    "LoadReport",
]
