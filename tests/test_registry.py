"""Unit tests for the registry: epochs and monotonic per-target sequences."""

import pytest

from mr_gateway.registry import (
    Registry, UnknownRobot,
    SEQ_ACCEPTED, SEQ_ROLLBACK, SEQ_DUPLICATE,
)


def make():
    return Registry()


def test_register_sets_initial_epoch_one():
    r = make()
    robot = r.register("alpha", "team/alpha")
    assert robot.epoch == 1
    assert robot.namespace == "team/alpha"


def test_remap_bumps_epoch_and_clears_watermarks():
    r = make()
    r.register("alpha", "team/alpha")
    r.accept_sequence("alpha", "cmd/move", 5)
    robot = r.register("alpha", "team/new_alpha")
    assert robot.epoch == 2
    assert robot.watermarks == {}


def test_same_namespace_reregister_still_bumps_epoch():
    # Any mapping change operation re-issues authorization; epoch is the
    # authority, so a no-op namespace POST also invalidates old tokens.
    r = make()
    r.register("alpha", "team/alpha")
    assert r.register("alpha", "team/alpha").epoch == 2


def test_sequence_monotonic_per_target():
    r = make()
    r.register("alpha", "team/alpha")
    assert r.accept_sequence("alpha", "cmd/move", 1).status == SEQ_ACCEPTED
    assert r.accept_sequence("alpha", "cmd/move", 3).status == SEQ_ACCEPTED
    rollback = r.accept_sequence("alpha", "cmd/move", 2)
    assert rollback.status == SEQ_ROLLBACK and rollback.previous == 3
    dup = r.accept_sequence("alpha", "cmd/move", 3)
    assert dup.status == SEQ_DUPLICATE and dup.previous == 3


def test_sequence_watermarks_are_independent_per_target():
    r = make()
    r.register("alpha", "team/alpha")
    r.accept_sequence("alpha", "cmd/move", 10)
    # other target starts fresh
    assert r.accept_sequence("alpha", "cmd/stop", 1).status == SEQ_ACCEPTED
    # rollback on the first target is still detected
    assert r.accept_sequence("alpha", "cmd/move", 9).status == SEQ_ROLLBACK


def test_sequence_watermarks_independent_per_robot():
    r = make()
    r.register("alpha", "team/alpha")
    r.register("beta", "team/beta")
    r.accept_sequence("alpha", "cmd/move", 10)
    assert r.accept_sequence("beta", "cmd/move", 1).status == SEQ_ACCEPTED


def test_get_unknown_robot_and_remove():
    r = make()
    with pytest.raises(UnknownRobot):
        r.get("ghost")
    r.register("alpha", "team/alpha")
    assert r.remove("alpha") is True
    assert r.exists("alpha") is False
    assert r.remove("alpha") is False


def test_fqn_is_namespaced():
    r = make()
    r.register("alpha", "team/alpha")
    fqn, robot = r.fqn("alpha", "cmd/move")
    assert fqn == "/team/alpha/cmd/move"
    with pytest.raises(UnknownRobot):
        r.fqn("ghost", "cmd/move")
