"""API tests: rotation, interruption, tampering, truncation over HTTP."""

import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ENVELOPE_DATA_DIR", str(tmp_path / "data"))
    import app.main as main

    importlib.reload(main)  # pick up the fresh data dir
    return TestClient(main.app)


def upload(client, data: bytes) -> str:
    resp = client.post("/objects", content=data)
    assert resp.status_code == 200
    return resp.json()["id"]


def test_upload_download_roundtrip(client):
    oid = upload(client, b"hello over http")
    resp = client.get(f"/objects/{oid}")
    assert resp.status_code == 200
    assert resp.content == b"hello over http"


def test_rotation_keeps_old_objects_decryptable(client):
    oid = upload(client, b"written before rotation")
    assert client.get(f"/objects/{oid}/info").json()["kek_version"] == 1

    resp = client.post("/keys/rotate")
    assert resp.json()["current"] == 2

    # old object, still wrapped with v1, must still decrypt
    assert client.get(f"/objects/{oid}").content == b"written before rotation"

    # re-wrap everything under v2; old object must STILL decrypt
    status = client.post("/rewrap").json()
    assert status["rewrapped"] == 1
    assert client.get(f"/objects/{oid}/info").json()["kek_version"] == 2
    assert client.get(f"/objects/{oid}").content == b"written before rotation"


def test_interrupted_rotation_leaves_all_objects_readable(client, monkeypatch):
    """Crash the rewrap loop halfway; every object must remain decryptable."""
    ids = [upload(client, f"object-{i}".encode()) for i in range(5)]
    client.post("/keys/rotate")

    import app.main as main

    real_put = main.objects.put
    calls = {"n": 0}

    def crashing_put(object_id, blob):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("simulated crash mid-rotation")
        real_put(object_id, blob)

    monkeypatch.setattr(main.objects, "put", crashing_put)
    with pytest.raises(RuntimeError):
        client.post("/rewrap")

    # 2 objects re-wrapped to v2, 3 still on v1 — all must decrypt
    versions = {
        client.get(f"/objects/{oid}/info").json()["kek_version"] for oid in ids
    }
    assert versions == {1, 2}
    for i, oid in enumerate(ids):
        assert client.get(f"/objects/{oid}").content == f"object-{i}".encode()

    # re-running the rewrap finishes the job
    status = client.post("/rewrap").json()
    assert status["rewrapped"] == 3
    for i, oid in enumerate(ids):
        assert client.get(f"/objects/{oid}").content == f"object-{i}".encode()


def test_tampered_header_returns_error_no_plaintext(client):
    secret = b"top secret payload"
    oid = upload(client, secret)

    import app.main as main

    blob = bytearray(main.objects.get(oid))
    blob[30] ^= 0x01  # corrupt the wrapped DEK in the header
    main.objects.put(oid, bytes(blob))

    resp = client.get(f"/objects/{oid}")
    assert resp.status_code == 422
    assert secret not in resp.content
    assert not resp.content.startswith(b"top")


def test_truncated_ciphertext_returns_error_no_plaintext(client):
    secret = b"a" * 200
    oid = upload(client, secret)

    import app.main as main

    blob = main.objects.get(oid)
    main.objects.put(oid, blob[:-10])  # cut into the GCM tag

    resp = client.get(f"/objects/{oid}")
    assert resp.status_code == 422
    # the response must not contain any prefix of the plaintext
    assert b"a" * 16 not in resp.content


def test_wrong_key_store_cannot_decrypt(client, tmp_path, monkeypatch):
    """Restarting against a fresh key store (old keys lost) must fail cleanly."""
    secret = b"cannot be recovered without the right KEK"
    oid = upload(client, secret)

    import app.main as main

    real_get = main.keys.get

    def wrong_key(version):
        real_get(version)
        return b"\x00" * 32  # a key of the right length but wrong value

    monkeypatch.setattr(main.keys, "get", wrong_key)
    resp = client.get(f"/objects/{oid}")
    assert resp.status_code == 422
    assert secret not in resp.content


def test_missing_object_404(client):
    assert client.get("/objects/doesnotexist").status_code == 404
