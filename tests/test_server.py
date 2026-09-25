"""End-to-end tests against a live localhost HTTP server (no network mocks)."""

import base64
import json
import socket
import urllib.request
import urllib.error

import pytest

from threshold_shares.server import create_server, MAX_BODY_BYTES
from conftest import flip_last_y


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server():
    httpd = create_server("127.0.0.1", _free_port())
    thread = __import__("threading").Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address[:2]
    yield f"http://{host}:{port}"
    httpd.shutdown()
    httpd.server_close()


def _post(server, path, payload):
    req = urllib.request.Request(
        server + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_healthz(server):
    with urllib.request.urlopen(server + "/healthz", timeout=5) as resp:
        body = json.loads(resp.read())
    assert body["status"] == "ok" and body["scheme_version"] == 1


def test_http_split_then_recover_any_3_of_5(server):
    secret = b"http-end-to-end"
    status, result = _post(server, "/v1/split", {
        "threshold": 3, "total": 5, "secret_b64": base64.b64encode(secret).decode()
    })
    assert status == 200
    assert len(result["shares"]) == 5

    status, recovered = _post(server, "/v1/recover", {
        "shares": [result["shares"][0], result["shares"][2], result["shares"][4]],
        "expected_fingerprint": result["secret_fingerprint"],
    })
    assert status == 200
    assert base64.b64decode(recovered["secret_b64"]) == secret
    assert recovered["used_indices"] == [1, 3, 5]
    assert recovered["fingerprint_matches"] is True


def test_http_text_secret(server):
    status, result = _post(server, "/v1/split", {
        "threshold": 2, "total": 3, "secret_text": "明文字符串 ✓"
    })
    assert status == 200
    status, recovered = _post(server, "/v1/recover", {"shares": result["shares"][:2]})
    assert status == 200
    assert base64.b64decode(recovered["secret_b64"]).decode() == "明文字符串 ✓"


def test_http_fewer_than_threshold_refused(server):
    _, result = _post(server, "/v1/split", {
        "threshold": 4, "total": 6, "secret_text": "needs four"
    })
    status, body = _post(server, "/v1/recover", {"shares": result["shares"][:3]})
    assert status == 400
    assert body["error"]["code"] == "insufficient_shares"
    assert body["error"]["details"]["missing"] == 1
    assert "secret_b64" not in json.dumps(body)


def test_http_mixed_batch_rejected(server):
    _, a = _post(server, "/v1/split", {"threshold": 3, "total": 5, "secret_text": "A"})
    _, b = _post(server, "/v1/split", {"threshold": 2, "total": 3, "secret_text": "B"})
    status, body = _post(server, "/v1/recover", {
        "shares": [a["shares"][0], a["shares"][1], b["shares"][0]]
    })
    assert status == 400
    assert body["error"]["code"] == "mixed_batch"


def test_http_corrupt_encoding_rejected(server):
    _, result = _post(server, "/v1/split", {"threshold": 3, "total": 5, "secret_text": "C"})
    status, body = _post(server, "/v1/recover", {
        "shares": [result["shares"][0], "SSS1$%%%not-base64", result["shares"][2]]
    })
    assert status == 400
    assert body["error"]["code"] == "invalid_share_encoding"
    assert body["error"]["details"]["errors"][0]["position"] == 1


def test_http_duplicate_index_rejected(server):
    _, result = _post(server, "/v1/split", {"threshold": 3, "total": 5, "secret_text": "D"})
    status, body = _post(server, "/v1/recover", {
        "shares": [result["shares"][0], result["shares"][0], result["shares"][1]]
    })
    assert status == 400
    assert body["error"]["code"] == "duplicate_share_index"


def test_http_fingerprint_mismatch_is_422_and_names_no_culprit(server):
    _, result = _post(server, "/v1/split", {"threshold": 3, "total": 5, "secret_text": "real"})
    tampered = flip_last_y(result["shares"][1])
    status, body = _post(server, "/v1/recover", {
        "shares": [result["shares"][0], tampered, result["shares"][2]],
        "expected_fingerprint": result["secret_fingerprint"],
    })
    assert status == 422
    assert body["error"]["code"] == "recovered_secret_invalid"
    assert "cannot identify which participant" in body["error"]["message"]


def test_http_json_object_shares_and_rejects_bad_types(server):
    _, result = _post(server, "/v1/split", {"threshold": 2, "total": 3, "secret_text": "E"})
    status, recovered = _post(server, "/v1/recover", {
        "shares": [result["shares_json"][0], result["shares_json"][2]]
    })
    assert status == 200
    assert base64.b64decode(recovered["secret_b64"]).decode() == "E"

    status, body = _post(server, "/v1/recover", {"shares": [1, 2, 3]})
    assert status == 400 and body["error"]["code"] == "invalid_parameters"


def test_http_bad_inputs(server):
    # invalid JSON
    req = urllib.request.Request(server + "/v1/split", data=b"{nope", method="POST")
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=5)
    assert exc.value.code == 400
    # unknown route
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(server + "/nope", timeout=5)
    assert exc.value.code == 404
    # bad parameters
    status, body = _post(server, "/v1/split", {"threshold": 9, "total": 3, "secret_text": "x"})
    assert status == 400 and body["error"]["code"] == "invalid_parameters"
    # missing secret selector
    status, body = _post(server, "/v1/split", {"threshold": 2, "total": 3})
    assert status == 400 and body["error"]["code"] == "invalid_parameters"
    # both secret selectors
    status, body = _post(server, "/v1/split", {
        "threshold": 2, "total": 3, "secret_text": "x", "secret_b64": "eA=="
    })
    assert status == 400 and body["error"]["code"] == "invalid_parameters"


def test_http_seal_and_unseal(server):
    _, result = _post(server, "/v1/split", {"threshold": 2, "total": 3, "secret_text": "wrap-me"})
    status, sealed = _post(server, "/v1/seal", {
        "share": result["shares"][0], "passphrase": "pw", "scrypt_n": 2**12
    })
    assert status == 200 and sealed["sealed_share"].startswith("SEAL1$")
    status, opened = _post(server, "/v1/unseal", {
        "sealed_share": sealed["sealed_share"], "passphrase": "pw"
    })
    assert status == 200 and opened["share"] == result["shares"][0]
    status, body = _post(server, "/v1/unseal", {
        "sealed_share": sealed["sealed_share"], "passphrase": "nope"
    })
    assert status == 400 and body["error"]["code"] == "sealing_error"


def test_http_body_size_limit(server):
    req = urllib.request.Request(
        server + "/v1/recover",
        data=b"{" + b'"x":"' + b"a" * (MAX_BODY_BYTES + 10) + b'"}',
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=10)
    assert exc.value.code == 413
