"""Temporal planning service.

Layered package:

- ``app.rules``    -- rule language: time arithmetic, conditions, action model
- ``app.planner``  -- reasoning/planning core (reference brute force + DFS solver)
- ``app.storage``  -- evidence store (SQLite) for runs, plans and replay traces
- ``app.api``      -- FastAPI query interface
"""

__version__ = "1.0.0"
