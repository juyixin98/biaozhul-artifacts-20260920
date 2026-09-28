"""Finite-domain CSP service for synthetic variable networks.

Layers:
- model:      rule language (problem schema, validation)
- kernel:     reasoning core (matching, all-different filtering, propagation, solver)
- evidence:   SQLite-backed evidence store (runs, events, solutions)
- api:        FastAPI query interface
- enumerate_: independent brute-force reference enumerator (verification only)
"""

__version__ = "0.1.0"
