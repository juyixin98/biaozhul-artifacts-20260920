"""Exception hierarchy for the TF cache."""


class TFError(Exception):
    """Base class for all tf_cache errors."""


class CycleError(TFError):
    """Adding the requested edge would create a cycle (or a second parent)."""


class LookupError_(TFError):
    """The requested frame is unknown or the requested time has no data."""


class ExtrapolationError(LookupError_):
    """The requested time lies outside the buffered range of an edge."""


class ConnectivityError(LookupError_):
    """No kinematic chain connects the two requested frames."""
