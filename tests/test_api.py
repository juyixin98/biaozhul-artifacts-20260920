"""HTTP API end-to-end tests."""

from __future__ import annotations

from pathlib import Path

DBC_TEXT = (Path(__file__).resolve().parents[1] / "examples" / "powertrain.dbc").read_text()


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_upload_and_list(client):
    r = client.get("/dbc")
    names = [d["name"] for d in r.json()]
    assert "powertrain" in names
    detail = client.get("/dbc/powertrain").json()
    assert detail["message_count"] == 4
    ids = [m["frame_id"] for m in detail["messages"]]
    assert 0x7FF in ids


def test_decode_engine_frame(client):
    r = client.post("/dbc/powertrain/decode", json={
        "frame_id": "0x100",
        "data": "28 0A 5C 64 00 00 00 00",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["message_name"] == "ENGINE_STATUS"
    assert body["frame_id_hex"] == "0x100"
    sigs = {s["name"]: s for s in body["signals"]}
    assert sigs["EngineSpeed"]["raw"] == 2600
    assert sigs["EngineSpeed"]["physical"] == 650.0
    assert sigs["EngineTemp"]["raw"] == 92
    assert sigs["EngineTemp"]["physical"] == 52.0
    assert len(body["dbc_version"]) == 64
    assert len(sigs["EngineSpeed"]["signal_version"]) == 64


def test_decode_negative_temp(client):
    r = client.post("/dbc/powertrain/decode", json={
        "frame_id": 256,
        "data": "0x1027E00000000000",
    })
    sigs = {s["name"]: s for s in r.json()["signals"]}
    assert sigs["EngineTemp"]["raw"] == -32
    assert sigs["EngineTemp"]["physical"] == -72.0


def test_decode_motorola_and_mux(client):
    r = client.post("/dbc/powertrain/decode", json={
        "frame_id": "0x200", "data": "1234F00000000000"
    })
    sigs = {s["name"]: s for s in r.json()["signals"]}
    assert sigs["VehicleSpeed"]["raw"] == 0x1234
    assert sigs["VehicleSpeed"]["physical"] == 46.60
    assert sigs["GearLever"]["raw"] == -1

    r = client.post("/dbc/powertrain/decode", json={
        "frame_id": "0x400", "data": "0084030000000000"
    })
    body = r.json()
    assert body["mux_value"] == 0
    sigs = {s["name"]: s for s in body["signals"]}
    assert sigs["Voltage"]["present"] and sigs["Voltage"]["physical"] == 0.9
    assert not sigs["Pressure"]["present"]


def test_decode_max_frame_id(client):
    r = client.post("/dbc/powertrain/decode", json={
        "frame_id": "0x7FF", "data": "5A000000FFFFFFFF"
    })
    assert r.status_code == 200, r.text
    sigs = {s["name"]: s for s in r.json()["signals"]}
    assert sigs["Counter"]["raw"] == 0xA
    assert sigs["BigMoto"]["raw"] == -1


def test_dlc_mismatch_rejected_and_logged(client):
    r = client.post("/dbc/powertrain/decode", json={
        "frame_id": "0x100", "data": "280A"
    })
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "frame_error"

    log = client.get("/log").json()
    assert log[0]["ok"] is False and "shorter than" in log[0]["error"]


def test_unknown_frame_rejected(client):
    r = client.post("/dbc/powertrain/decode", json={
        "frame_id": "0x333", "data": "00" * 8
    })
    assert r.status_code == 422


def test_frame_id_out_of_standard_range(client):
    r = client.post("/dbc/powertrain/decode", json={
        "frame_id": "0xFFF", "data": "00" * 8
    })
    assert r.status_code == 422
    assert "11-bit" in r.json()["detail"]["message"]


def test_invalid_hex_odd_nibbles(client):
    r = client.post("/dbc/powertrain/decode", json={
        "frame_id": "0x100", "data": "ABC"
    })
    assert r.status_code == 422


def test_hex_forms_equivalent(client):
    body = {"frame_id": "0x100"}
    forms = [
        "28 0A 5C 64 00 00 00 00",
        "0x28 0x0A 0x5C 0x64 0x00 0x00 0x00 0x00",
        "280a5c6400000000",
        "0x280a5c6400000000",
        "28,0A,5C,64,00,00,00,00",
    ]
    raws = set()
    for data in forms:
        r = client.post("/dbc/powertrain/decode", json={**body, "data": data})
        assert r.status_code == 200, (data, r.text)
        raws.add(tuple((s["name"], s["raw"]) for s in r.json()["signals"]))
    assert len(raws) == 1


def test_create_invalid_dbc_rejected(client):
    bad = (
        'VERSION ""\n\nNS_:\n\tNS_DESC_\n\nBS_:\n\nBU_: A\n\n'
        "BO_ 100 M: 1 A\n"
        ' SG_ S : 0|99@1+ (1,0) [0|0] "" A\n'
    )
    r = client.post("/dbc", json={"name": "bad", "content": bad})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "dbc_parse_error"


def test_extended_id_upload_rejected(client):
    bad = (
        'VERSION ""\n\nNS_:\n\tNS_DESC_\n\nBS_:\n\nBU_: A\n\n'
        "BO_ 2147483904 M: 1 A\n"   # 0x80000100
        ' SG_ S : 0|1@1+ (1,0) [0|0] "" A\n'
    )
    r = client.post("/dbc", json={"name": "ext", "content": bad})
    assert r.status_code == 400
    assert "extended" in r.json()["detail"]["message"]


def test_duplicate_name_conflict_then_replace(client):
    r = client.post("/dbc", json={"name": "powertrain", "content": DBC_TEXT})
    assert r.status_code == 409
    r = client.post("/dbc", json={
        "name": "powertrain", "content": DBC_TEXT, "replace": True
    })
    assert r.status_code == 201


def test_bitmap_endpoint(client):
    r = client.get("/dbc/powertrain/bitmap/1024")
    assert r.status_code == 200
    text = r.text
    assert "Frame 0x400" in text
    assert "ServiceId" in text


def test_delete_and_404(client):
    r = client.delete("/dbc/powertrain")
    assert r.status_code == 204
    r = client.get("/dbc/powertrain")
    assert r.status_code == 404
