"""Rule language: a small, explicitly validated DSL for ATMS problems.

The language has four statement forms, one per line (``#`` starts a
comment)::

    assume A, B            # declared assumption nodes
    fact F                 # unconditional premise node
    rule r1: A, B => C     # Horn justification
    rule r2: A, C => FALSE # deriving FALSE marks the environment nogood

Justifications are Horn clauses.  The only form of negation is deriving
the reserved ``FALSE`` node: any environment whose assumptions force
``FALSE`` is a contradiction (nogood).  This keeps semantics monotone,
which is exactly what label propagation needs.
"""
