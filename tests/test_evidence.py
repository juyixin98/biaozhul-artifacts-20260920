"""Evidence chain: append-only, hash-linked, tamper-evident."""
import json

from app import evidence


def test_chain_valid_after_ingest(client):
    client.post("/v1/sessions", json={"session_id": "ev", "initial_soc": 0.5})
    client.post("/v1/sessions/ev/samples", json={"samples": [
        {"t_s": float(t), "current_a": 1.0, "voltage_v": 3.8, "temp_c": 25.0}
        for t in range(0, 10)
    ]})
    body = client.get("/v1/sessions/ev/evidence").json()
    assert body["chain_valid"] is True
    types = [r["type"] for r in body["records"]]
    assert types == ["session_created", "ingest"]
    rec = body["records"][-1]
    assert rec["anchor_verified"] is True
    assert rec["accepted"] == 10
    # hashes really linked
    assert body["records"][1]["prev_hash"] == body["records"][0]["hash"]


def test_tampering_detected(tmp_path):
    path = tmp_path / "evidence.jsonl"
    evidence.append_record(path, {"type": "a", "value": 1})
    evidence.append_record(path, {"type": "b", "value": 2})
    assert evidence.verify_chain(path) is True

    lines = path.read_text().splitlines()
    rec = json.loads(lines[0])
    rec["value"] = 999  # tamper
    lines[0] = json.dumps(rec, sort_keys=True)
    path.write_text("\n".join(lines) + "\n")
    assert evidence.verify_chain(path) is False


def test_deletion_detected(tmp_path):
    path = tmp_path / "evidence.jsonl"
    for k in range(3):
        evidence.append_record(path, {"type": "x", "k": k})
    lines = path.read_text().splitlines()
    path.write_text("\n".join(lines[:1] + lines[2:]) + "\n")  # drop middle record
    assert evidence.verify_chain(path) is False
