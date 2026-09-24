"""End-to-end HTTP API tests via FastAPI TestClient + SQLite persistence."""

from __future__ import annotations

import hmac
import hashlib
import json

import pytest


def test_health(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"


def test_decode_without_definition_is_404ish(client):
    response = client.post("/decode", json={
        "frame_id": 256, "data": "00 00 00 00 00 00 00 00"
    })
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "no_definition"


def test_upload_returns_version_and_signature(uploaded_client):
    response = uploaded_client.post("/dbc", json={"dbc": open(
        "examples/demo.dbc", encoding="utf-8").read()})
    body = response.json()
    assert response.status_code == 200
    assert len(body["version_id"]) == 64
    assert all(c in "0123456789abcdef" for c in body["version_id"])
    assert len(body["signature_sha256"]) == 64
    assert body["signature_algorithm"] == "HMAC-SHA256"
    assert body["message_count"] == 3


def test_upload_is_idempotent_on_identical_dbc(client):
    text = open("examples/demo.dbc", encoding="utf-8").read()
    first = client.post("/dbc", json={"dbc": text}).json()
    second = client.post("/dbc", json={"dbc": text}).json()
    assert first["version_id"] == second["version_id"]
    assert first["created"] is True
    assert second["created"] is False


def test_comment_only_change_keeps_version_format_change_changes_it(
    uploaded_client,
):
    text = open("examples/demo.dbc", encoding="utf-8").read()
    base = uploaded_client.post("/dbc", json={"dbc": text}).json()["version_id"]

    # Whitespace/comment edits do not affect the *semantic* fingerprint...
    commented = text.replace(
        'CM_ BO_ 256', 'CM_ BO_ 256', 1
    ) + '\nCM_ SG_ 256 EngineSpeed "extra comment";\n'
    with_comment = uploaded_client.post(
        "/dbc", json={"dbc": commented}
    ).json()["version_id"]
    assert with_comment == base

    # ...but changing a factor does.
    changed = text.replace("(0.25,0)", "(0.5,0)", 1)
    changed_id = uploaded_client.post(
        "/dbc", json={"dbc": changed}
    ).json()["version_id"]
    assert changed_id != base


def test_decode_engine_frame(uploaded_client, version_id):
    response = uploaded_client.post("/decode", json={
        "frame_id": 256,
        "data": [0x34, 0x12, 0xD8, 0x64, 0, 0, 0, 0],
    })
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["frame_id_hex"] == "0x100"
    assert body["definition_version"] == version_id
    by_name = {s["name"]: s for s in body["signals"]}
    assert by_name["EngineSpeed"] == {
        "name": "EngineSpeed", "raw": 4660, "value": 1165.0,
        "unit": "rpm", "length": 16, "byte_order": "intel", "signed": False,
        "mux_kind": "plain", "mux_value": None,
        "definition_version": version_id,
    }
    assert by_name["EngineTemp"]["value"] == -80.0


def test_decode_hex_string_without_spaces_and_0x(uploaded_client):
    response = uploaded_client.post("/decode", json={
        "frame_id": 256, "data": "0x3412D86400000000"
    })
    assert response.status_code == 200
    assert response.json()["data_hex"] == "34 12 D8 64 00 00 00 00"


def test_decode_odd_hex_digits_rejected(uploaded_client):
    response = uploaded_client.post("/decode", json={
        "frame_id": 256, "data": "ABC"
    })
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "bad_hex"


def test_decode_non_hex_rejected(uploaded_client):
    response = uploaded_client.post("/decode", json={
        "frame_id": 256, "data": "ZZ 00 00 00 00 00 00 00"
    })
    assert response.status_code == 422


def test_decode_frame_id_too_large_rejected_by_schema(client):
    response = client.post("/decode", json={
        "frame_id": 0x800, "data": "00"
    })
    assert response.status_code == 422  # pydantic validation


def test_unknown_frame_id_returns_404(uploaded_client):
    response = uploaded_client.post("/decode", json={
        "frame_id": 400, "data": "00 00 00 00 00 00 00 00"
    })
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "unknown_message"


def test_dlc_too_short_returns_422(uploaded_client):
    response = uploaded_client.post("/decode", json={
        "frame_id": 300, "data": "01 00 40 00 00 00 00"
    })
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "dlc_too_short"


def test_bad_dbc_upload_line_reported(client):
    response = client.post("/dbc", json={"dbc": "BO_ 1 X: 9 A\n"})
    assert response.status_code == 422
    detail = response.json()["error"]
    assert detail["code"] == "dbc_syntax_error"
    assert "DLC" in detail["message"]


def test_extended_multiplex_dbc_rejected(client):
    dbc = (
        'VERSION "x"\nNS_ :\n BS_:\nBU_: A\n'
        'BO_ 1 X: 4 A\n SG_ S m1m0 : 0|8@1+ (1,0) [0|1] "" A\n'
    )
    response = client.post("/dbc", json={"dbc": dbc})
    assert response.status_code == 422
    assert "extended multiplexing" in response.json()["error"]["message"]


def test_mux_decode_and_persistence_then_history(uploaded_client):
    response = uploaded_client.post("/decode", json={
        "frame_id": 300, "data": "01 00 40 06 00 00 00 00"
    })
    body = response.json()
    assert body["mux"] == 1
    assert {s["name"] for s in body["signals"]} == {
        "BrakeMux", "PedalPos", "BrakePress"
    }
    stored = body["stored_frame_id"]
    assert isinstance(stored, int)

    history = uploaded_client.get(
        f"/frames?frame_id=300&limit=10"
    ).json()
    assert history["count"] == 1
    row = history["frames"][0]
    assert row["stored_frame_id"] == stored
    assert row["mux"] == 1
    assert any(s["name"] == "BrakePress" and s["raw"] == 1600
               for s in row["signals"])


def test_persist_false_leaves_no_history(uploaded_client):
    response = uploaded_client.post("/decode", json={
        "frame_id": 256,
        "data": [0] * 8,
        "persist": False,
    })
    assert response.status_code == 200
    assert response.json()["stored_frame_id"] is None
    assert uploaded_client.get("/frames").json()["count"] == 0


def test_version_pins_the_definition(uploaded_client, version_id):
    # Upload a second DBC that lacks frame 256; explicit version_id must
    # still decode against the pinned first definition.
    other = (
        'VERSION "other"\nNS_ :\n BS_:\nBU_: A\n'
        'BO_ 100 Other: 1 A\n SG_ S : 0|8@1+ (1,0) [0|255] "" A\n'
    )
    uploaded_client.post("/dbc", json={"dbc": other})
    response = uploaded_client.post("/decode", json={
        "frame_id": 256,
        "data": [0] * 8,
        "version_id": version_id,
    })
    assert response.status_code == 200
    assert response.json()["message_name"] == "EngineStatus"


def test_hmac_signature_verifies_and_tampering_fails(uploaded_client):
    text = open("examples/demo.dbc", encoding="utf-8").read()
    upload = uploaded_client.post("/dbc", json={"dbc": text}).json()
    vid, sig = upload["version_id"], upload["signature_sha256"]

    ok = uploaded_client.post("/verify", json={
        "version_id": vid, "signature": sig
    }).json()
    assert ok["valid"] is True
    assert ok["algorithm"] == "HMAC-SHA256"

    forged = sig[:-1] + ("0" if sig[-1] != "0" else "1")
    bad = uploaded_client.post("/verify", json={
        "version_id": vid, "signature": forged
    }).json()
    assert bad["valid"] is False

    # Independently recompute the HMAC with the persisted key and compare.
    import app.main as main
    key = main._store.hmac_key()
    expected = hmac.new(key, vid.encode(), hashlib.sha256).hexdigest()
    assert hmac.compare_digest(expected, sig)


def test_hmac_constant_signature_across_restarts_with_persisted_key(
    tmp_path, monkeypatch
):
    """The auto-generated key lives in SQLite, so signatures survive reload."""

    from fastapi.testclient import TestClient
    from app import main

    db_file = tmp_path / "k.db"
    monkeypatch.setenv("CANDECODE_DB", str(db_file))
    text = open("examples/demo.dbc", encoding="utf-8").read()

    main._db_cache.clear()
    main._store = None
    with TestClient(main.app) as c1:
        sig1 = c1.post("/dbc", json={"dbc": text}).json()["signature_sha256"]

    # Simulate a second process: same DB path, fresh in-memory state.
    main._db_cache.clear()
    main._store = None
    with TestClient(main.app) as c2:
        sig2 = c2.post("/dbc", json={"dbc": text}).json()["signature_sha256"]
    assert sig1 == sig2


def test_definition_lookup_endpoint(uploaded_client, version_id):
    response = uploaded_client.get(f"/definitions/{version_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["message_count"] == 3
    assert body["dbc_version"] == "demo-dbc-v1"
    assert "ECU_ENG" in body["nodes"]


def test_all_example_payload_cases(uploaded_client):
    """Run the exact cases from examples/payloads.jsonl over HTTP."""

    cases = [
        json.loads(line)
        for line in open("examples/payloads.jsonl", encoding="utf-8")
        if line.strip()
    ]
    failures = []
    for i, case in enumerate(cases, 1):
        expect = case.pop("_expect", None)
        expect_error = case.pop("_expect_error", None)
        expect_status = case.pop("_expect_http_status", None)
        response = uploaded_client.post("/decode", json=case)
        if expect_status is not None:
            if response.status_code != expect_status:
                failures.append((i, f"status {response.status_code}"))
            continue
        if expect_error:
            if response.json().get("error", {}).get("code") != \
                    expect_error["code"]:
                failures.append((i, response.text))
            continue
        assert response.status_code == 200, (i, response.text)
        result = response.json()
        signals = {s["name"]: s for s in result["signals"]}
        for name, fields in expect.items():
            if name in ("message_name", "mux", "_absent"):
                continue
            for key, value in fields.items():
                if signals[name][key] != value:
                    failures.append(
                        (i, f"{name}.{key}={signals[name][key]}!={value}")
                    )
        for absent in expect.get("_absent", []):
            if absent in signals:
                failures.append((i, f"{absent} should be absent"))
    assert not failures, failures
