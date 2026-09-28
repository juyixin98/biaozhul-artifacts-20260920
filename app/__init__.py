"""Two-group two-period DID and event-time descriptive service.

Modules
-------
- contracts:   statistical contract (request/response models, failure categories)
- panel:       object identity alignment across periods, fixed-weight balancing
- kernel:      estimation core (four-cell means, weighted DID, clustered SE)
- reference:   *independent* closed-form OLS cross-check (separate code path)
- diagnostics: evidence/diagnostics (pre-trend, contamination), never "proof"
- event:       event-time alignment / FE event study with explicit support refusal
- experiments: deterministic, hand-authored reproducibility fixtures
- storage:     SQLite audit trail keyed by request identity
- api:         FastAPI wiring
"""

__version__ = "1.0.0"
