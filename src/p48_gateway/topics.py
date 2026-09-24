"""Strict name validation and safe topic construction.

The gateway only ever publishes into *registered* namespaces and only onto a
fixed allow-list of relative topic names. Everything that can be influenced by
an external request is funnelled through the validators below so that an
absolute topic (``/foo``), a private/tilde topic (``~/foo``), an empty segment
or a parent-segment escape (``..``) can never reach ``create_publisher``.
"""

from __future__ import annotations

import re

# Robot ids are the only external handle used to address a robot.
ROBOT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
# One path segment: ROS-name characters only. "." and ".." never match.
SEGMENT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
# A relative topic name may contain several segments separated by "/".
REL_TOPIC_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_/]{0,127}$")

# Fixed, gateway-wide relative topic names. Clients can never supply a topic.
CMD_TOPIC = "cmd"
EPOCH_TOPIC = "epoch"
STATUS_TOPIC = "status"
EVENTS_TOPIC = "events"
ALLOWED_TOPICS = frozenset({CMD_TOPIC, EPOCH_TOPIC, STATUS_TOPIC, EVENTS_TOPIC})


class TopicError(ValueError):
    """Raised when a name would break namespace containment."""


def validate_robot_id(robot_id: object) -> str:
    if not isinstance(robot_id, str) or not ROBOT_ID_RE.fullmatch(robot_id):
        raise TopicError(f"invalid robot id: {robot_id!r}")
    return robot_id


def validate_namespace(namespace: object) -> str:
    """Validate an absolute, contained namespace such as ``/p48/alpha``.

    The namespace must start with ``/``, must not end with ``/`` and every
    segment has to be a plain ROS name.  Absolute roots, ``.``/``..``, ``~``
    and empty segments are all rejected.
    """
    if not isinstance(namespace, str) or not namespace.startswith("/"):
        raise TopicError(f"namespace must start with '/': {namespace!r}")
    if len(namespace) > 256 or namespace.endswith("/"):
        raise TopicError(f"invalid namespace: {namespace!r}")
    for segment in namespace[1:].split("/"):
        if not SEGMENT_RE.fullmatch(segment):
            raise TopicError(
                f"invalid namespace segment {segment!r} in {namespace!r} "
                "(absolute paths, '.', '..', '~' and empty segments are forbidden)"
            )
    return namespace


def validate_relative_topic(name: object) -> str:
    """Validate a *relative* topic name (no leading ``/`` or ``~``)."""
    if not isinstance(name, str) or not REL_TOPIC_RE.fullmatch(name):
        raise TopicError(f"invalid relative topic: {name!r}")
    segments = name.split("/")
    if any(seg in ("", ".", "..", "~") for seg in segments):
        raise TopicError(f"topic escapes its namespace: {name!r}")
    return name


def join_topic(namespace: str, relative_topic: str) -> str:
    """Join a validated namespace with a validated fixed relative topic.

    The returned string is the fully-qualified topic used for logging. The
    nodes themselves publish with the *relative* name while living in the
    namespace, so containment is structural, not just string based.
    """
    validate_namespace(namespace)
    if relative_topic not in ALLOWED_TOPICS:
        raise TopicError(f"topic {relative_topic!r} is not on the allow-list")
    validate_relative_topic(relative_topic)
    return f"{namespace}/{relative_topic}"
