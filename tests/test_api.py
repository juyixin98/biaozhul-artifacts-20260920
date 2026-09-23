"""HTTP API tests: multipart verification, receipts, persistence, signatures."""

import json

from provenance_service.crypto import canonical_json, verify_signature
from provenance_service.fixtures import sample_job_parts


def _files(parts, *, package=None, config=None, output=None, expected=None):
    return {
        "package": ("sources.zip",
                    package if package is not None else parts["package"],
                    "application/zip"),
        "config": (None, config if config is not None
                   else json.dumps(parts["config"])),
        "compiler_output": (None, output if output is not None
                            else json.dumps(parts["compiler_output"])),
        "expected_runtime_code": (None,
                                  json.dumps(parts["expected_runtime_code"])
                                  if expected is None else expected),
    }


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert len(bytes.fromhex(body["signing_key"])) == 32


def test_verify_exact(client, happy_parts):
    r = client.post("/api/v1/verify", files=_files(happy_parts))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["verdict"] == "EXACT_MATCH"
    assert body["job_id"]
    assert body["receipt"]["signature"]


def test_get_job_findings_and_receipt(client, happy_parts):
    job = client.post("/api/v1/verify", files=_files(happy_parts)).json()
    jid = job["job_id"]

    got = client.get(f"/api/v1/jobs/{jid}")
    assert got.status_code == 200
    assert got.json()["verdict"] == "EXACT_MATCH"

    findings = client.get(f"/api/v1/jobs/{jid}/findings").json()["findings"]
    assert any(f["code"] == "METADATA_HASH_OK" for f in findings)
    assert any(f["code"] == "SCRIPT_NOT_EXECUTED" for f in findings)

    receipt = client.get(f"/api/v1/jobs/{jid}/receipt").json()
    assert receipt["signature_valid"] is True
    # Independently re-verify the signature with the public key.
    msg = canonical_json({
        "job_id": receipt["job_id"],
        "payload_digest": receipt["payload_digest"],
        "prev_chain": receipt["prev_chain"],
        "public_key": receipt["public_key"],
    })
    assert verify_signature(receipt["public_key"], msg,
                            bytes.fromhex(receipt["signature"]))


def test_chain_links_across_jobs(client, happy_parts):
    j1 = client.post("/api/v1/verify", files=_files(happy_parts)).json()
    j2 = client.post("/api/v1/verify", files=_files(happy_parts)).json()
    assert j1["receipt"]["prev_chain"] is None
    assert j2["receipt"]["prev_chain"] == j1["evidence"]["receipt_chain_head"]
    listing = client.get("/api/v1/jobs").json()["jobs"]
    assert [j["job_id"] for j in listing[:2]] == [j2["job_id"], j1["job_id"]]
    assert client.get("/health").json()["chain_head"] == j2["evidence"]["receipt_chain_head"]


def test_rejected_package_is_persisted_with_422(client, happy_parts, tmp_path):
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../../etc/evil", b"x")
    files = _files(happy_parts, package=buf.getvalue())
    r = client.post("/api/v1/verify", files=files)
    assert r.status_code == 422
    body = r.json()
    assert body["status"] == "REJECTED"
    assert client.get(f"/api/v1/jobs/{body['job_id']}").status_code == 200


def test_bad_json_returns_400(client, happy_parts):
    files = _files(happy_parts, config="{not json")
    assert client.post("/api/v1/verify", files=files).status_code == 400
    files = _files(happy_parts, output="not json")
    assert client.post("/api/v1/verify", files=files).status_code == 400


def test_unknown_job_404(client):
    assert client.get("/api/v1/jobs/deadbeef").status_code == 404


def test_library_address_rule_match_over_http(client, happy_parts):
    from provenance_service.fixtures import link_object
    other = "0xAb5801a7D398351b8bE11C439e05C5B3259aeC9B"
    art = happy_parts["compiler_output"]["contracts"]["src/Token.sol"]["Token"][
        "evm"]["deployedBytecode"]["object"]
    expected = {happy_parts["fqn"]: link_object(art, happy_parts["lib_fqn"], other)}
    files = _files(happy_parts, expected=json.dumps(expected))
    body = client.post("/api/v1/verify", files=files).json()
    assert body["verdict"] == "RULE_MATCH"
