"""Exact rational linear-equation and rank service.

Modules
-------
errors        : error taxonomy and exception types (the cross-module contract)
frac          : exact-number protocol (Fraction / mpmath rational adapters)
numeric_input : boundary parsing/validation of JSON numeric payloads
kernel        : fraction-free Bareiss elimination core (exact integers only)
evidence      : independent back-substitution / rank / near-singularity evidence
runner        : digit budgets, run ids, intermediate checkpoints, replayable logs
api           : FastAPI service interface
replay        : command-line replayer for past runs
"""

__version__ = "0.1.0"
