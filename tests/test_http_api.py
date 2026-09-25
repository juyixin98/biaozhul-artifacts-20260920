"""End-to-end tests over real HTTP on an ephemeral localhost port."""

import base64
import json
import threading

import pytest

from tl import merkle
from tl.client import LogClient, unb64
from tl.server import build_log, serve


@pytest.fixture()
def client(tmp_path):
    tlog = build_log(str(tmp_path))
    httpd = serve(tlog, "127.0.0.1", 0)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield LogClient(f"http://127.0.0.1:{port}")
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=2)


def test_health_and_empty_sth(client):
    h = client.health()
    assert h == {"status": "ok", "tree_size": 0}
    sth = client.sth()
    assert sth["tree_size"] == 0
    assert base64.b64decode(sth["root_hash"]) == merkle.EMPTY_TREE_HASH
    assert client.verify_sth(sth)


def test_full_workflow_over_http(client):
    # 7 entries -> exercises the RFC 9162 7-leaf non-power-of-two shape
    indices = [client.add_text(f"entry-{i}") for i in range(7)]
    assert indices == list(range(7))

    sth = client.sth()
    assert sth["tree_size"] == 7
    assert client.verify_sth(sth)

    # fetch an entry and verify its inclusion proof locally
    for idx in range(7):
        proof = client.inclusion(idx)
        assert proof["tree_size"] == 7
        assert client.verify_inclusion_response(proof)

    entry = client.entry(3)
    assert base64.b64decode(entry["data_b64"]) == b"entry-3"


def test_server_rejects_forged_root_over_http(client):
    client.add(b"aaa")
    client.add(b"bbb")
    proof = client.inclusion(0)
    forged = b"\x00" * 32
    body = {
        "kind": "inclusion",
        "leaf_index": 0,
        "tree_size": 2,
        "leaf_hash": proof["leaf_hash"],
        "root_hash": base64.b64encode(forged).decode(),
        "proof": proof["proof"],
    }
    assert client.verify_server(body) == {"verified": False}

    # wrong index: server must reject as well
    body["root_hash"] = proof["root_hash"]
    body["leaf_index"] = 1
    assert client.verify_server(body) == {"verified": False}

    # the genuine proof verifies
    body["leaf_index"] = 0
    assert client.verify_server(body) == {"verified": True}


def test_consistency_over_http(client):
    for i in range(3):
        client.add(bytes([i]))
    old_sth = client.sth()
    for i in range(3, 7):
        client.add(bytes([i]))
    con = client.consistency(3)
    assert con["old_size"] == 3 and con["new_size"] == 7
    assert client.verify_consistency_response(con)

    # tamper old root -> rejected by both local check and server
    bad = dict(con)
    bad["old_root"] = base64.b64encode(b"\x11" * 32).decode()
    assert not client.verify_consistency_response(bad)
    body = {
        "kind": "consistency",
        "old_size": 3,
        "new_size": 7,
        "old_root": bad["old_root"],
        "new_root": con["new_root"],
        "proof": con["proof"],
    }
    assert client.verify_server(body) == {"verified": False}


def test_error_handling(client):
    import urllib.error

    # bad index on proof endpoint (tree is empty -> index 5 invalid)
    with pytest.raises(urllib.error.HTTPError) as ei:
        client.inclusion(5)
    assert ei.value.code == 400
    assert "index" in json.loads(ei.value.read())["error"]

    # missing entry
    with pytest.raises(urllib.error.HTTPError) as ei:
        client.entry(0)
    assert ei.value.code == 404

    # malformed POST
    import urllib.request
    req = urllib.request.Request(
        client.base_url + "/v1/entries",
        data=b"not-json",
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with pytest.raises(urllib.error.HTTPError) as ei:
        urllib.request.urlopen(req)
    assert ei.value.code == 400

    # unknown route
    with pytest.raises(urllib.error.HTTPError) as ei:
        client._get("/nope")
    assert ei.value.code == 404


def test_public_key_endpoint(client):
    key = client.public_key()
    assert key["algorithm"] == "Ed25519"
    raw = unb64(key["public_key_b64"])
    assert len(raw) == 32
    assert "BEGIN PUBLIC KEY" in key["public_key_pem"]
    assert key["sth_context"] == "TL-STH-v1"
