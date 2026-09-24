"""Domain errors for trajectory evaluation.

Every error is explicit: nothing is padded with zeros, silently dropped
or "fixed" in a way that could make the reported error look smaller.
"""


class TrajectoryError(ValueError):
    """Base class for all 400-level evaluation failures."""

    code = "trajectory_error"


class DuplicateTimestampError(TrajectoryError):
    """A single trajectory contains a repeated timestamp."""

    code = "duplicate_timestamp"


class ZeroMatchesError(TrajectoryError):
    """No estimate/ground-truth pair lies within the time tolerance."""

    code = "zero_matches"


class DegenerateAlignmentError(TrajectoryError):
    """The matched geometry cannot determine the requested transform."""

    code = "degenerate_alignment"


class InvalidOrientationError(TrajectoryError):
    """A quaternion is zero-norm (or otherwise invalid)."""

    code = "invalid_orientation"


class InvalidRequestError(TrajectoryError):
    """Request parameters are inconsistent (empty spans, etc.)."""

    code = "invalid_request"
