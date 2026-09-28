"""Rete matching-network core.

Objects and data flow::

    WME (working-memory element)
      -> AlphaNetwork (constant tests, per-type alpha memories with indexes)
      -> JoinNode  (right activation probes left BetaMemory)
      -> BetaMemory (partial/complete tokens, indexed by variable and by WME)
      -> terminal JoinNode -> Agenda (priority + stable-key ordering)
      -> Engine.fire (bounded, explicit outcome; actions never loop forever)
"""

from .agenda import Activation, Agenda
from .engine import Engine, FireReport
from .network import ReteNetwork

__all__ = ["Activation", "Agenda", "Engine", "FireReport", "ReteNetwork"]
