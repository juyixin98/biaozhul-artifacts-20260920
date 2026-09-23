"""Finality Divergence Detector — pure backend service.

A simplified weighted-vote finality gadget:

* The validator set is fixed per epoch.
* A checkpoint becomes final only when signed by validators whose combined
  weight is *strictly greater* than 2/3 of the total epoch weight.
* Every quorum comparison is done with integers (``3 * power > 2 * total``),
  never with floating point.
* A validator that signs two distinct values for the same (epoch, height)
  commits an equivocation ("double vote"): both signatures are kept as
  conflict evidence and the validator's whole weight is excluded for that
  epoch.
* Different epochs never share tallies.
* Once an epoch is final its value is immutable; a conflicting finalized
  value raises an alarm and freezes progress — it is never silently
  overwritten by a newer message.
* Votes, evidence, checkpoints and alarms are persisted in SQLite so a
  restart rebuilds the exact same conclusion.
"""

__all__ = ["__version__"]
__version__ = "1.0.0"
