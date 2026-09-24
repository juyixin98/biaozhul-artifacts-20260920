import time

import pytest

from p48_gateway.crypto import canonical_json, hmac_sign
from p48_gateway.protocol import build_command_envelope, verify_command_envelope

EPOCH_KEY = "ioksSseGEHLK3_AT1mQIr2bI5w2gAv-EwTSdm2g7ZPY"
TKEY = "6SkRyx5Fb07IwBAyLkR0y1pSajsbWoG0avrbibiQNP4"
OTHER_TKEY = "-jy51chNDGv8X56cp3X7xYKqXz6LMpD1rdr9p1ECn8A"
KEYS = {"tester_alpha": TKEY, "tester_bravo": OTHER_TKEY}


def make_env(**overrides):
    kwargs = dict(
        robot_id="alpha",
        namespace="/p48/alpha",
        target="dock",
        seq=1,
        command={"pose": [1.0, 2.0]},
        expires_at=time.time() + 10,
        issued_at=time.time(),
        epoch=1,
        tester="tester_alpha",
        tester_key=TKEY,
        command_id="cid-1",
    )
    kwargs.update(overrides)
    return build_command_envelope(**kwargs)


def verify(env, **kw):
    params = dict(
        expected_robot_id="alpha",
        expected_namespace="/p48/alpha",
        current_epoch=1,
        tester_keys=KEYS,
        now=time.time(),
        seen_ids=set(),
    )
    params.update(kw)
    return verify_command_envelope(env, **params)


def test_valid_envelope_passes():
    ok, reason = verify(make_env())
    assert ok, reason


def test_tampered_signature_rejected():
    env = make_env(seq=2)
    env["seq"] = 3
    ok, reason = verify(env)
    assert not ok and reason == "bad_signature"


def test_wrong_tester_key_rejected():
    env = make_env(tester="tester_alpha", tester_key=OTHER_TKEY)
    ok, reason = verify(env)
    assert not ok and reason == "bad_signature"


def test_unknown_tester_rejected():
    env = make_env()
    ok, reason = verify(env, tester_keys={"other": TKEY})
    assert not ok and reason == "unknown_tester"


def test_stale_epoch_rejected():
    env = make_env(epoch=1)
    ok, reason = verify(env, current_epoch=2)
    assert not ok and reason == "stale_epoch"


def test_robot_and_namespace_binding():
    env = make_env(robot_id="bravo", namespace="/p48/bravo")
    ok, reason = verify(env)
    assert not ok and reason == "robot_mismatch"

    env2 = make_env(namespace="/p48/renamed")
    ok, reason = verify(env2)
    assert not ok and reason == "namespace_mismatch"


def test_expiry_checked_purely():
    env = make_env(expires_at=time.time() - 0.001)
    ok, reason = verify(env)
    assert not ok and reason == "expired_on_arrival"


def test_duplicate_delivery_flagged():
    env = make_env()
    seen = {"cid-1"}
    ok, reason = verify(env, seen_ids=seen)
    assert not ok and reason == "duplicate_delivery"
    ok2, _ = verify(env, seen_ids=set())
    assert ok2


def test_malformed_inputs():
    ok, reason = verify("not a dict")
    assert not ok and reason == "malformed_json"
    env = make_env()
    del env["sig"]
    ok, reason = verify(env)
    assert not ok and reason == "malformed_envelope"
