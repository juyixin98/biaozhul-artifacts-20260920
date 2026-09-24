"""Naming rules for robot identifiers, namespaces and topic names.

Security boundary
-----------------
The gateway accepts a short *robot id* and a *target* topic from callers and
maps them onto a ROS 2 topic of the form ``/<namespace>/<target>``.  Both
pieces of input are treated as hostile:

* a target starting with ``/`` would be an **absolute** ROS 2 name and escape
  the robot namespace entirely;
* a target containing ``..`` / ``~`` could resolve outside the namespace;
* a namespace containing ``/`` or ``.`` could cross into another namespace or
* the graph root.

``qualify_topic`` is the single chokepoint: it validates both inputs and
returns a relative-looking absolute FQN fully under the registered namespace.
"""

from __future__ import annotations

import re

# Robot id: stable handle used in API paths. Conservative charset, length
# bounded. Must itself never be interpolated into a ROS name as-is (the
# registered namespace is what is used).
_ROBOT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

# A single ROS 2 name token: alnum / underscore, may start with a letter or
# underscore. We deliberately do NOT accept '~', braces substitutions or
# spaces. ROS allows more, but the gateway's wire contract is deliberately
# narrower.
_TOKEN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")

_MAX_TOPIC_DEPTH = 8
_MAX_NAMESPACE_DEPTH = 4
_MAX_TOPIC_LEN = 256


class NamingError(ValueError):
    """Raised when an id, namespace or target topic violates naming policy."""


def validate_robot_id(robot_id: str) -> None:
    if not isinstance(robot_id, str) or not _ROBOT_ID_RE.match(robot_id):
        raise NamingError(
            f"invalid robot id {robot_id!r}: must match {_ROBOT_ID_RE.pattern}"
        )


def _validate_token(token: str, kind: str) -> None:
    if not isinstance(token, str) or not _TOKEN_RE.match(token):
        raise NamingError(
            f"invalid {kind} segment {token!r}: must match {_TOKEN_RE.pattern}"
        )


def validate_namespace(namespace: str) -> None:
    """Validate a namespace body.

    Accepted forms: ``team1``, ``team1/alpha`` (relative, slash-joined tokens).
    Rejected: leading ``/`` (absolute), empty segments, ``.``/``..``, ``~``.
    """
    if not isinstance(namespace, str) or not namespace:
        raise NamingError("namespace must be a non-empty string")
    if len(namespace) > _MAX_TOPIC_LEN:
        raise NamingError("namespace too long")
    if namespace.startswith("/"):
        raise NamingError(
            f"namespace {namespace!r} must not start with '/' (no absolute names)"
        )
    segments = namespace.split("/")
    if len(segments) > _MAX_NAMESPACE_DEPTH:
        raise NamingError(
            f"namespace depth {len(segments)} exceeds {_MAX_NAMESPACE_DEPTH}"
        )
    for seg in segments:
        if seg in ("", ".", ".."):
            raise NamingError(f"namespace {namespace!r} contains illegal segment")
        _validate_token(seg, "namespace")


def validate_target(target: str) -> None:
    """Validate a caller-supplied *relative* target topic (no namespace)."""
    if not isinstance(target, str) or not target:
        raise NamingError("target must be a non-empty string")
    if len(target) > _MAX_TOPIC_LEN:
        raise NamingError("target too long")
    if target.startswith("/"):
        raise NamingError(
            f"target {target!r} must not start with '/' (absolute topics are "
            "forbidden: a command may only publish inside the robot namespace)"
        )
    if target.startswith("~"):
        raise NamingError(
            f"target {target!r} must not start with '~' (private-name escape)"
        )
    segments = target.split("/")
    if len(segments) > _MAX_TOPIC_DEPTH:
        raise NamingError(f"target depth {len(segments)} exceeds {_MAX_TOPIC_DEPTH}")
    for seg in segments:
        if seg in ("", ".", ".."):
            raise NamingError(f"target {target!r} contains illegal segment")
        _validate_token(seg, "target")


def qualify_topic(namespace: str, target: str) -> str:
    """Return the fully-qualified topic ``/<namespace>/<target>``.

    Both inputs are re-validated here even if the caller validated them, so no
    code path can bypass the checks. Guarantees the returned FQN begins with
    ``/<namespace>/`` — i.e. it cannot resolve outside the robot namespace.
    """
    validate_namespace(namespace)
    validate_target(target)
    return "/" + namespace.strip("/") + "/" + target.strip("/")
