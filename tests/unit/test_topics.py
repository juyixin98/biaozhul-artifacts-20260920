import pytest

from p48_gateway.topics import (
    TopicError,
    join_topic,
    validate_namespace,
    validate_relative_topic,
    validate_robot_id,
)


def test_valid_namespaces():
    assert validate_namespace("/p48/alpha") == "/p48/alpha"
    assert validate_namespace("/p48/a1_b2") == "/p48/a1_b2"
    assert validate_namespace("/ns") == "/ns"


@pytest.mark.parametrize(
    "bad",
    [
        "p48/alpha",      # not absolute
        "/p48/",          # trailing slash / empty segment
        "/p48//alpha",    # empty segment
        "/p48/../secret", # parent escape
        "/p48/./x",       # dot segment
        "/p48/~",         # tilde
        "/p48/a-b",       # illegal char
        "/123",           # segment must start with a letter
        "",
        None,
        42,
    ],
)
def test_bad_namespaces_rejected(bad):
    with pytest.raises(TopicError):
        validate_namespace(bad)


@pytest.mark.parametrize("name", ["cmd", "status", "events", "p48/cmd"])
def test_valid_relative_topics(name):
    assert validate_relative_topic(name) == name


@pytest.mark.parametrize(
    "bad",
    [
        "/p48/cmd",   # absolute path
        "~/cmd",      # private
        "../cmd",     # parent escape
        "cmd/../x",
        "cmd//x",
        "cmd/",
        "",
        None,
    ],
)
def test_bad_relative_topics_rejected(bad):
    with pytest.raises(TopicError):
        validate_relative_topic(bad)


@pytest.mark.parametrize("rid", ["alpha", "robot-1", "R_2"])
def test_valid_robot_ids(rid):
    assert validate_robot_id(rid) == rid


@pytest.mark.parametrize("rid", ["", "a/b", ".", "..", "../x", "a b", "x" * 33])
def test_bad_robot_ids(rid):
    with pytest.raises(TopicError):
        validate_robot_id(rid)


def test_join_topic_allow_list():
    assert join_topic("/p48/alpha", "cmd") == "/p48/alpha/cmd"
    with pytest.raises(TopicError):
        join_topic("/p48/alpha", "../cmd")
    with pytest.raises(TopicError):
        join_topic("/p48/alpha", "not_allowed")
