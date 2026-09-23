"""HTTP 协议层：错误码、缺字段、畸形输入、列表/详情路由。"""

import json

import pytest

from tests.conftest import submit_dir


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["ok"] is True


def _base_data(examples):
    d = examples / "01-exact"
    return {
        "config": (d / "config.json").read_text(),
        "compilerOutput": (d / "compilerOutput.json").read_text(),
        "targetDeployed": (d / "target_deployed.hex").read_text(),
    }


def test_missing_config_400(client, examples):
    data = _base_data(examples)
    del data["config"]
    with open(examples / "01-exact" / "sources.zip", "rb") as fh:
        r = client.post(
            "/api/v1/verify",
            files={"package": ("x.zip", fh, "application/zip")},
            data=data,
        )
    assert r.status_code == 400 and r.json()["error"]["code"] == "MISSING_FIELD"


def test_missing_sources_400(client, examples):
    data = _base_data(examples)
    r = client.post("/api/v1/verify", data=data)
    assert r.status_code == 400 and r.json()["error"]["code"] == "MISSING_SOURCES"


def test_bad_json_400(client, examples):
    data = _base_data(examples)
    data["config"] = "{not json"
    r = client.post(
        "/api/v1/verify",
        data={**data, "sources": json.dumps({"src/Vault.sol": "x"})},
    )
    assert r.status_code == 400 and r.json()["error"]["code"] == "BAD_JSON"


def test_bad_target_hex_400(client, examples):
    data = _base_data(examples)
    data["targetDeployed"] = "0xZZ"
    with open(examples / "01-exact" / "sources.zip", "rb") as fh:
        r = client.post(
            "/api/v1/verify", files={"package": ("x.zip", fh, "application/zip")}, data=data
        )
    assert r.status_code == 400 and r.json()["error"]["code"] == "BAD_TARGET_BYTECODE"


def test_inline_source_conflict_with_archive_400(client, examples):
    data = _base_data(examples)
    data["sources"] = json.dumps({"src/Vault.sol": "// conflicting"})
    with open(examples / "01-exact" / "sources.zip", "rb") as fh:
        r = client.post(
            "/api/v1/verify", files={"package": ("x.zip", fh, "application/zip")}, data=data
        )
    assert r.status_code == 400 and r.json()["error"]["code"] == "SOURCE_CONFLICT"


def test_unsafe_inline_path_400(client, examples):
    data = _base_data(examples)
    data["sources"] = json.dumps({"../escape.sol": "x"})
    r = client.post("/api/v1/verify", data=data)
    assert r.status_code == 400 and r.json()["error"]["code"] == "BAD_SOURCES"


@pytest.mark.parametrize(
    "name",
    ["zipslip.zip", "absolute.zip", "backslash.zip", "nul.zip", "symlink.tar", "hardlink.tar", "duplicate.zip"],
)
def test_malicious_packages_rejected_over_http(client, examples, name):
    data = _base_data(examples)
    with open(examples / "_malicious" / name, "rb") as fh:
        r = client.post(
            "/api/v1/verify",
            files={"package": (name, fh, "application/octet-stream")},
            data=data,
        )
    assert r.status_code == 400 and r.json()["error"]["code"] == "BAD_PACKAGE"


def test_list_and_detail_routes(client, examples):
    submit_dir(client, examples / "01-exact")
    submit_dir(client, examples / "06-mismatch-semantic")
    jobs = client.get("/api/v1/jobs").json()["jobs"]
    assert len(jobs) == 2
    assert {j["verdict"] for j in jobs} == {"EXACT", "MISMATCH"}
    jid = jobs[0]["id"]
    assert client.get(f"/api/v1/jobs/{jid}").status_code == 200
    assert client.get(f"/api/v1/jobs/{jid}/report").status_code == 200
    assert client.get(f"/api/v1/jobs/{jid}/evidence").status_code == 200
    assert client.get("/api/v1/jobs/zzz/report").status_code == 404


def test_job_id_path_traversal_not_lookup(client, storage):
    # job id 必须是 hex；伪造 id 不能读取任意路径
    assert storage.get_job("../../etc/passwd") is None
    assert client.get("/api/v1/jobs/..%2F..%2Fetc/report").status_code in (404, 422)
