"""Persistence layer (SQLite evidence store).

The store is an *append-only audit trail* - it never reconstructs engine
state, it records what happened so a reviewer can answer:

* which rule set/version produced a result (``runs``, ``rules``);
* which facts were inserted/retracted, by whom and when (``facts``);
* which activations existed, in what agenda order, with which source facts
  (``activations``);
* which activations fired, what they asserted/retracted (``firings``);
* the ordered structured trace with the exact decision basis (``trace``).

SQLite is accessed through one short-lived connection per operation with
``check_same_thread=False`` guarded by a lock; in-memory databases share the
same connection for their lifetime.
"""

from .evidence import EvidenceStore

__all__ = ["EvidenceStore"]
