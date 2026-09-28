"""STRIPS-style offline planning service.

Module boundaries (see docs/contracts.md):
    model / parser / validation / grounding - rule language
    semantics / search / heuristics         - planning kernel
    executor                                - independent plan verifier
    evidence                                - run-evidence store (SQLite)
    service / api                           - query interface
"""

__version__ = "1.0.0"
