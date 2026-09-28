"""Small weighted finite-state transducer (WFST) service.

Layered package layout:

* ``wfst_service.corpus``  -- corpus/specification layer: symbols, JSON schema,
  validation, lexicon -> transducer expansion, bundled synthetic fixtures.
* ``wfst_service.core``    -- mining/maths kernel: immutable FST model,
  epsilon closure, epsilon-filtered composition, cycle analysis and
  bounded shortest-output enumeration.
* ``wfst_service.index``   -- persistence layer: SQLite schema + repository.
* ``wfst_service.api``     -- query validation / transport layer (FastAPI).
"""

__version__ = "1.0.0"
