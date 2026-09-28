"""Error taxonomy for tenmem.

Every failure that crosses a module boundary is represented by one of the
subclasses of :class:`TenmemError`.  The ``category`` string is the stable
contract used by the HTTP layer (status code mapping), the run log
(replay/filtering) and the tests (assertions on failure *category*, not just on
"some exception was raised").

Categories
----------
input_error           malformed graph / feed / request (caller's fault, 422)
planning_error        no valid memory plan can be built (422)
replanning_required   runtime shape exceeds the plan's capacity; the caller
                      must replan with new bounds — never reuse an undersized
                      buffer (409)
state_conflict        illegal lifecycle/state transition, alias/buffer
                      conflict (409)
resource_exhausted    plan/execution exceeds the byte budget (507)
computation_failed    a numerical kernel raised at execution time (500)
"""


class TenmemError(Exception):
    category = "error"

    def __init__(self, message: str, *, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = dict(details or {})

    def to_dict(self) -> dict:
        return {"category": self.category, "message": self.message, "details": self.details}


class GraphValidationError(TenmemError):
    category = "input_error"


class PlanningError(TenmemError):
    category = "planning_error"


class ReplanningRequiredError(TenmemError):
    """Raised at execution when a concrete shape exceeds planned capacity.

    It is a *state* problem (the existing plan cannot safely serve this run),
    not an input grammar error and not an OOM: the remedy is to replan with the
    observed bounds. Old buffers must never be reused past their capacity.
    """

    category = "replanning_required"


class StateConflictError(TenmemError):
    category = "state_conflict"


class ResourceExhaustedError(TenmemError):
    category = "resource_exhausted"


class ComputationError(TenmemError):
    category = "computation_failed"


# HTTP status code per category (single source of truth for the API layer).
STATUS_BY_CATEGORY = {
    "input_error": 422,
    "planning_error": 422,
    "replanning_required": 409,
    "state_conflict": 409,
    "resource_exhausted": 507,
    "computation_failed": 500,
    "error": 500,
}
