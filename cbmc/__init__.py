"""Contract bounded model checker (CBMC-style) built on Z3.

Pure backend: explicit JSON finite-state contract models, bounded checks of
balance non-negativity and total conservation, shortest counterexample traces,
and concrete step-by-step replay. No user code is ever executed: the contract
language is a closed whitelist of expression operators.
"""

from .checker import CheckResult, StepTrace, check_contract
from .errors import ContractError, ReplayError
from .fixtures import get_fixture, list_fixtures, load_example
from .model import Contract, load_contract
from .replay import ReplayReport, replay_trace

__all__ = [
    "CheckResult",
    "StepTrace",
    "Contract",
    "ContractError",
    "ReplayError",
    "ReplayReport",
    "check_contract",
    "load_contract",
    "load_example",
    "list_fixtures",
    "get_fixture",
    "replay_trace",
]
