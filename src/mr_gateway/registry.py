"""In-memory robot registry: identity -> namespace mapping with epochs.

The registry is the authority on which robot namespaces exist. The ROS bridge
only publishes for namespaces present here.

Each robot carries a monotonic ``epoch``: bumped on every register/remap so
that tokens issued against the previous mapping are immediately stale.

Per-target sequence watermarks enforce "same target, sequence never moves
backwards". Watermarks are stored here (policy state), separately from the
ROS layer (transport).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .naming import NamingError, qualify_topic, validate_namespace, validate_robot_id


@dataclass
class Robot:
    robot_id: str
    namespace: str
    epoch: int
    # target topic -> highest accepted sequence number
    watermarks: dict[str, int] = field(default_factory=dict)
    created_at: float = 0.0
    updated_at: float = 0.0


class UnknownRobot(KeyError):
    pass


# Sequence decision outcomes
SEQ_ACCEPTED = "accepted"
SEQ_ROLLBACK = "rollback"
SEQ_DUPLICATE = "duplicate"


@dataclass(frozen=True)
class SequenceResult:
    status: str
    previous: int | None  # current high-water mark when not accepted



class Registry:
    def __init__(self, *, clock: Callable[[], float] | None = None) -> None:
        self._robots: dict[str, Robot] = {}
        self._lock = threading.RLock()
        self._clock = clock or time.time

    # ---- admin operations -------------------------------------------------

    def register(self, robot_id: str, namespace: str) -> Robot:
        """Insert a robot or remap an existing one.

        A new robot starts at epoch 1. Remapping bumps the epoch, clears
        sequence watermarks (the new namespace is a fresh session) and
        therefore invalidates every outstanding token for that robot.
        """
        validate_robot_id(robot_id)
        validate_namespace(namespace)
        with self._lock:
            existing = self._robots.get(robot_id)
            now = self._clock()
            if existing is None:
                robot = Robot(robot_id=robot_id, namespace=namespace, epoch=1,
                              created_at=now, updated_at=now)
                self._robots[robot_id] = robot
                changed = True
            else:
                changed = existing.namespace != namespace
                existing.namespace = namespace
                existing.epoch += 1
                existing.watermarks.clear()
                existing.updated_at = now
                robot = existing
            return robot

    def remove(self, robot_id: str) -> bool:
        with self._lock:
            return self._robots.pop(robot_id, None) is not None

    # ---- queries ----------------------------------------------------------

    def get(self, robot_id: str) -> Robot:
        with self._lock:
            robot = self._robots.get(robot_id)
            if robot is None:
                raise UnknownRobot(robot_id)
            # Return a shallow snapshot copy so callers cannot mutate state.
            return Robot(
                robot_id=robot.robot_id,
                namespace=robot.namespace,
                epoch=robot.epoch,
                watermarks=dict(robot.watermarks),
                created_at=robot.created_at,
                updated_at=robot.updated_at,
            )

    def exists(self, robot_id: str) -> bool:
        with self._lock:
            return robot_id in self._robots

    def list(self) -> list[Robot]:
        with self._lock:
            return [
                Robot(
                    robot_id=r.robot_id,
                    namespace=r.namespace,
                    epoch=r.epoch,
                    watermarks=dict(r.watermarks),
                    created_at=r.created_at,
                    updated_at=r.updated_at,
                )
                for r in self._robots.values()
            ]

    # ---- sequence policy --------------------------------------------------

    def accept_sequence(self, robot_id: str, target: str,
                        sequence: int) -> SequenceResult:
        """Atomically validate and record a sequence number.

        * ``SEQ_ACCEPTED``  — watermark advanced (including the first one);
        * ``SEQ_ROLLBACK``  — sequence lower than the high-water mark, the
          watermark is left untouched and the command is rejected;
        * ``SEQ_DUPLICATE`` — sequence equal to the high-water mark; rejected
          so a resent command can never be executed twice.
        """
        if not isinstance(sequence, int) or isinstance(sequence, bool):
            raise NamingError("sequence must be an integer")
        if sequence < 0:
            raise NamingError("sequence must be non-negative")
        with self._lock:
            robot = self._robots.get(robot_id)
            if robot is None:
                raise UnknownRobot(robot_id)
            prev = robot.watermarks.get(target)
            if prev is not None:
                if sequence < prev:
                    return SequenceResult(SEQ_ROLLBACK, prev)
                if sequence == prev:
                    return SequenceResult(SEQ_DUPLICATE, prev)
            robot.watermarks[target] = sequence
            return SequenceResult(SEQ_ACCEPTED, prev)

    def fqn(self, robot_id: str, target: str) -> tuple[str, Robot]:
        """Resolve a (robot, target) to its validated absolute ROS topic.

        Raises :class:`UnknownRobot` / :class:`NamingError`.
        """
        with self._lock:
            robot = self._robots.get(robot_id)
            if robot is None:
                raise UnknownRobot(robot_id)
            return qualify_topic(robot.namespace, target), robot
