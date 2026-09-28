"""Bounded HTN task planner.

Modules
-------
- ``htn_planner.lang``      rule language: domain/problem parsing, condition
  evaluation, operator/method schema validation.
- ``htn_planner.core``      planning kernel: bounded recursive decomposition,
  method selection, ordered + partially-ordered subtask expansion.
- ``htn_planner.store``     evidence store: SQLite-backed persistence of
  planning runs, expansion trees and failure evidence.
- ``htn_planner.api``       query interface: FastAPI application exposing
  plan/inspect endpoints.
- ``htn_planner.verify``    independent verification: re-checks that every
  primitive action in a plan is executable in sequence and that hierarchy
  constraints hold; does not reuse the planner's own decisions.
"""

__version__ = "0.1.0"
