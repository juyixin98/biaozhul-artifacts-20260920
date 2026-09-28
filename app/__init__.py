"""ATMS teaching backend.

An assumption-based truth maintenance system (ATMS) exposed over FastAPI
with a SQLite evidence store. Package layout:

- app.core    -- reasoning kernel (labels, environments, nogoods, budgets)
- app.rules   -- rule language (schema + validation for Horn-style rules)
- app.store   -- SQLite evidence store (sessions, operation log, diagnostics)
- app.api     -- query interface (FastAPI routes, request ids, diagnostics)
- app.config  -- budgets and runtime settings
"""

__version__ = "0.1.0"
