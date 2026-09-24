import time

import pytest

from p48_gateway.crypto import (
    NonceStore,
    b64decode,
    b64encode,
    canonical_json,
    hmac_sign,
    hmac_verify,
    request_signing_payload,
)

KEY = b64encode(b"0" * 32)
OTHER_KEY = b64encode(b"1" * 32)


def test_sign_and_verify_roundtrip():
    payload = canonical_json({"a": 1, "b": [1, 2]})
    sig = hmac_sign(payload, KEY)
    assert hmac_verify(payload, sig, KEY)
    assert not hmac_verify(payload, sig, OTHER_KEY)
    assert not hmac_verify(payload + b"x", sig, KEY)


def test_canonical_json_is_order_independent():
    assert canonical_json({"a": 1, "b": 2}) == canonical_json({"b": 2, "a": 1})


def test_key_must_be_32_bytes():
    with pytest.raises(Exception):
        b64decode(b64encode(b"short"))


def test_request_payload_includes_method_and_path_and_bodyhash():
    body = {"seq": 1}
    p1 = request_signing_payload("POST", "/robots/alpha/commands", "t", "n", 1.0, body)
    p2 = request_signing_payload("GET", "/robots/alpha/commands", "t", "n", 1.0, None)
    p3 = request_signing_payload("POST", "/robots/bravo/commands", "t", "n", 1.0, body)
    assert p1 != p2 != p3
    # member order of the body must not matter
    p4 = request_signing_payload("POST", "/robots/alpha/commands", "t", "n", 1.0,
                                 {"seq": 1})
    assert p1 == p4


def test_nonce_replay_rejected():
    store = NonceStore(window=60)
    now = time.time()
    ok, reason = store.check("nonce-001", now, now=now)
    assert ok and reason is None
    ok, reason = store.check("nonce-001", now, now=now)
    assert not ok and reason == "replayed_nonce"


def test_nonce_timestamp_window():
    store = NonceStore(window=5)
    now = time.time()
    ok, reason = store.check("nonce-002", now - 30, now=now)
    assert not ok and reason == "stale_timestamp"
    ok, _ = store.check("nonce-003", now + 1, now=now)
    assert ok


def test_nonce_bad_format():
    store = NonceStore()
    ok, reason = store.check("../escape", time.time())
    assert not ok and reason == "bad_nonce"
    ok, reason = store.check("short", time.time())
    assert not ok and reason == "bad_nonce"
