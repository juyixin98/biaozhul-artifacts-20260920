"""Unit tests for naming policy (absolute-path and cross-namespace escape)."""

import pytest

from mr_gateway.naming import (
    NamingError,
    qualify_topic,
    validate_namespace,
    validate_robot_id,
    validate_target,
)


def test_happy_path_fqn_is_under_namespace():
    assert qualify_topic("team/alpha", "cmd/move") == "/team/alpha/cmd/move"
    assert qualify_topic("alpha", "cmd_move") == "/alpha/cmd_move"


@pytest.mark.parametrize("bad", [
    "/cmd/move",       # absolute topic
    "~/private",       # private-name escape
    "../global",       # parent traversal
    "cmd/../escape",   # embedded traversal
    "cmd//move",       # empty segment
    "/",               # graph root
    "cmd/.",           # dot segment
    "ns{ns}/x",        # substitution
])
def test_target_rejects_escape(bad):
    with pytest.raises(NamingError):
        validate_target(bad)


@pytest.mark.parametrize("bad", [
    "/team/alpha",     # absolute namespace
    "team//alpha",     # empty segment
    "../team",         # traversal
    "team/..",         # trailing traversal
    ".",               # current node
    "",                # empty
])
def test_namespace_rejects_escape(bad):
    with pytest.raises(NamingError):
        validate_namespace(bad)


@pytest.mark.parametrize("rid", [
    "alpha", "robot-1", "a_b", "r1", "x" * 32,
])
def test_valid_robot_ids(rid):
    validate_robot_id(rid)


@pytest.mark.parametrize("rid", [
    "", "/alpha", "alpha/beta", "..", "alpha.", "-alpha", "ALPHA",
    "x" * 33, "alpha beta",
])
def test_invalid_robot_ids(rid):
    with pytest.raises(NamingError):
        validate_robot_id(rid)


def test_qualify_always_prefixes_registered_namespace():
    # Even a target that *looks* absolute-ish after encoding cannot escape:
    for attempt in ("/other_ns/cmd", "../../etc", "~/secret"):
        with pytest.raises(NamingError):
            qualify_topic("team/alpha", attempt)
