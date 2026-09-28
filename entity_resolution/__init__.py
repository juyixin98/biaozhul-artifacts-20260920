"""Synthetic organization-name entity resolution backend.

Modules
-------
errors   : categorical error taxonomy shared across layer boundaries.
models   : data + error contracts (pydantic v2).
normalization : alias resolution and cross-language canonicalization.
similarity    : pairwise name similarity and per-pair candidate evidence.
clustering    : conflict-checked must/cannot-link constraints and correlation clustering.
storage       : SQLite persistence (records, links, locks, runs, audit log).
service       : application facade / orchestration and change-impact reporting.
api           : FastAPI HTTP surface with explicit error envelopes.
diagnostics   : run journals (replayable intermediate state + rationale).

The core design invariant: *pairwise similarity never implies transitivity*.
Edges are soft evidence; only explicit must-link / cannot-link constraints are
hard, and constraints are validated for contradiction before they are applied.
"""

from __future__ import annotations

__all__ = ["__version__"]
__version__ = "1.0.0"
