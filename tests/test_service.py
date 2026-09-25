"""Tests for the loopback HTTP JSON service (in-process, no real sockets
to non-local hosts; ThreadingHTTPServer on 127.0.0.1)."""

from __future__ import annotations

import json
import threading
from urllib import request as urlrequest
from urllib.error import HTTPError

import pytest

from certverifier import service as service_module
from cryptography.hazmat.primitives import serialization


@pytest.fixture(scope="module")
def server():
    srv = service_module.create_server("127.0.0.1", 0)  # ephemeral port
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=5)


def _url(server, path="/verify"):
    host, port = server.server_address
    return f"http://{host}:{port}{path}"


def _post(server, payload):
    data = json.dumps(payload).encode("utf-8")
    req = urlrequest.Request(
        _url(server), data=data,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urlrequest.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _body(sc, **overrides):
    def pem(c):
        return c.public_bytes(serialization.Encoding.PEM).decode()
    payload = {
        "leaf_certificate": pem(sc.leaf),
        "intermediates": "".join(pem(c) for c in sc.all_intermediates),
        "trust_anchors": "".join(pem(c) for c in sc.anchors),
        "verification_time": sc.verification_time.isoformat().replace("+00:00", "Z"),
        "purpose": sc.purpose,
        "hostname": sc.hostname,
    }
    payload.update(overrides)
    return payload


def test_health(server):
    with urlrequest.urlopen(_url(server, "/health"), timeout=5) as resp:
        body = json.loads(resp.read())
    assert body["status"] == "ok"
    assert body["revocation_checking"] == "disabled-by-design (offline)"


def test_good_request_valid(server, scenarios):
    status, body = _post(server, _body(scenarios["good"]))
    assert status == 200
    assert body["valid"] is True
    assert body["revocation"]["checked"] is False


@pytest.mark.parametrize("name", [
    "expired", "path_length_violation", "non_ca_intermediate",
    "same_name_rogue_root", "hostname_mismatch", "untrusted_root",
])
def test_acceptance_scenarios_invalid(server, scenarios, name):
    sc = scenarios[name]
    status, body = _post(server, _body(sc))
    assert status == 200  # verification failure is still a successful call
    assert body["valid"] is False, name
    codes = {f["code"] for f in body["findings"]}
    assert codes & sc.expected_codes, (name, codes)


def test_missing_anchor_rejected(server, scenarios):
    body = _body(scenarios["good"])
    body.pop("trust_anchors")
    status, payload = _post(server, body)
    assert status == 400
    assert payload["error"]["code"] == "MISSING_TRUST_ANCHORS"


def test_bad_time_rejected(server, scenarios):
    status, payload = _post(server, _body(scenarios["good"], verification_time="not-a-date"))
    assert status == 400
    assert payload["error"]["code"] == "INVALID_TIME"


def test_invalid_json_rejected(server):
    req = urlrequest.Request(
        _url(server), data=b"{not json",
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        urlrequest.urlopen(req, timeout=5)
        assert False
    except HTTPError as exc:
        assert exc.code == 400
        assert json.loads(exc.read())["error"]["code"] == "INVALID_JSON"


def test_refuses_non_loopback_bind():
    with pytest.raises(ValueError, match="local-only"):
        service_module.create_server("0.0.0.0", 0)
