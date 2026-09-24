"""Time-indexed storage of SE(3) samples for a single TF edge."""

from __future__ import annotations

import bisect

from .exceptions import ExtrapolationError, LookupError_
from .transform import SE3

# Tolerance (seconds) applied when checking whether a query time falls inside
# the buffered range, to absorb floating-point noise at the boundaries.
TIME_EPS = 1e-9


class TransformBuffer:
    """A sorted series of (time, SE3) samples for one parent->child edge.

    Lookup rules:
      * empty buffer            -> LookupError_
      * exactly one sample      -> treated as a static transform, valid at
                                   any time (like ROS ``/tf_static``)
      * two or more samples     -> linear interpolation of translation and
                                   SLERP of rotation inside the range;
                                   queries outside [t_min, t_max] raise
                                   ExtrapolationError (never extrapolated)
    """

    __slots__ = ("_times", "_transforms")

    def __init__(self) -> None:
        self._times: list[float] = []
        self._transforms: list[SE3] = []

    def __len__(self) -> int:
        return len(self._times)

    @property
    def times(self) -> list[float]:
        return list(self._times)

    def insert(self, time: float, transform: SE3) -> None:
        """Insert a sample; a sample at the same time is replaced."""
        time = float(time)
        idx = bisect.bisect_left(self._times, time)
        if idx < len(self._times) and self._times[idx] == time:
            self._transforms[idx] = transform
            return
        self._times.insert(idx, time)
        self._transforms.insert(idx, transform)

    def lookup(self, time: float) -> SE3:
        """Return the transform at ``time``, interpolating if needed."""
        time = float(time)
        n = len(self._times)
        if n == 0:
            raise LookupError_("edge has no data")
        if n == 1:
            # Static transform: a single sample is valid at every time.
            return self._transforms[0]
        t0, t1 = self._times[0], self._times[-1]
        if time < t0 - TIME_EPS or time > t1 + TIME_EPS:
            raise ExtrapolationError(
                f"requested time {time!r} outside buffered range [{t0!r}, {t1!r}]"
            )
        idx = bisect.bisect_left(self._times, time)
        if idx < n and self._times[idx] == time:
            return self._transforms[idx]
        # idx is the first sample strictly after `time` (idx >= 1 here,
        # because time > t0 and time < t1 were checked above).
        ta, tb = self._times[idx - 1], self._times[idx]
        alpha = (time - ta) / (tb - ta)
        return self._transforms[idx - 1].interpolate(self._transforms[idx], alpha)
