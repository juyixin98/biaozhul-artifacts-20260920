"""Estimation kernels.

Each kernel exposes a *power function* over integer allocations plus a
continuous (analytic) starting estimate.  Kernels never round to "an answer":
the planning layer is responsible for the integer search and the
n-passes / n-1-fails verification.
"""
