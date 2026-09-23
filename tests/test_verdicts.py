"""端到端：8 个样例的裁决必须与预期一致；差异不得被规范化掩盖。"""

import json

from tests.conftest import load_variant, submit_dir


def test_all_generated_variants(client, examples):
    expected_map = {}
    for vdir in sorted(examples.iterdir()):
        if not vdir.is_dir() or vdir.name.startswith("_"):
            continue
        r = submit_dir(client, vdir)
        payload = json.loads((vdir / "README.json").read_text())
        expected = payload["expected_verdict"]
        expected_map[vdir.name] = expected
        if expected == "HTTP_400":
            assert r.status_code == 400, (vdir.name, r.text)
            assert r.json()["error"]["code"] == "BAD_LIBRARY_ADDRESS"
        else:
            assert r.status_code == 200, (vdir.name, r.text)
            got = r.json()["report"]["verdict"]
            assert got == expected, f"{vdir.name}: expected {expected}, got {got}"


def test_exact_has_no_rule_and_no_diffs(client, examples):
    r = submit_dir(client, examples / "01-exact")
    rep = r.json()["report"]
    assert rep["verdict"] == "EXACT"
    comp = [c for c in rep["checks"] if c["code"] == "BYTECODE_COMPARISON"][0]
    assert comp["detail"]["unexplained_diffs"] == []
    assert comp["severity"] == "INFO"
    assert comp["detail"]["masked_difference_observed"] is False


def test_library_address_rule_match_details(client, examples):
    r = submit_dir(client, examples / "02-rule-libaddr")
    rep = r.json()["report"]
    assert rep["verdict"] == "RULE_MATCH"
    assert "LINKED_LIBRARY_ADDRESSES" in rep["rules_applied"]
    comp = [c for c in rep["checks"] if c["code"] == "BYTECODE_COMPARISON"][0]
    assert comp["detail"]["unexplained_diffs"] == []
    assert comp["detail"]["masked_difference_observed"] is True
    linking = [c for c in rep["checks"] if c["code"] == "LIBRARY_LINKING"][0]
    assert linking["detail"]["deployed_links"][0]["address"] == "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed"


def test_metadata_only_rule_match(client, examples):
    r = submit_dir(client, examples / "03-rule-metadata")
    rep = r.json()["report"]
    assert rep["verdict"] == "RULE_MATCH"
    assert rep["rules_applied"] == ["CBOR_METADATA_TAIL"]


def test_optimizer_setting_mismatch_is_not_normalized_away(client, examples):
    """关键反掩盖用例：runs 200 vs 999999 必须判 MISMATCH。"""
    r = submit_dir(client, examples / "04-mismatch-optimizer")
    rep = r.json()["report"]
    assert rep["verdict"] == "MISMATCH"
    check = [c for c in rep["checks"] if c["code"] == "COMPILATION_SETTINGS"][0]
    dis = check["detail"]["disagreements"]
    assert any(d["key"] == "optimize_runs" for d in dis)


def test_missing_source_is_mismatch_not_inconclusive(client, examples):
    r = submit_dir(client, examples / "05-missing-source")
    assert r.json()["report"]["verdict"] == "MISMATCH"
    codes = [c["code"] for c in r.json()["report"]["checks"]]
    assert "SOURCE_PRESENT" in codes


def test_semantic_code_change_is_mismatch(client, examples):
    r = submit_dir(client, examples / "06-mismatch-semantic")
    rep = r.json()["report"]
    assert rep["verdict"] == "MISMATCH"
    comp = [c for c in rep["checks"] if c["code"] == "BYTECODE_COMPARISON"][0]
    assert comp["detail"]["unexplained_diffs"], "主体代码差异绝不能被元数据/库掩码掩盖"


def test_compiler_version_disagreement(client, examples):
    r = submit_dir(client, examples / "07-mismatch-version")
    rep = r.json()["report"]
    assert rep["verdict"] == "MISMATCH"
    check = [c for c in rep["checks"] if c["code"] == "COMPILER_VERSION"][0]
    assert check["severity"] == "FAIL"
    # CBOR solc 三段与 metadata 版本也被交叉核验
    assert check["detail"]["cbor_solc"] == [0, 8, 20]


def test_inline_sources_submission(client, examples):
    """只提供 sources JSON（无归档）也应可核验。"""
    data = load_variant(examples / "01-exact")
    import zipfile

    with zipfile.ZipFile(examples / "01-exact" / "sources.zip") as zf:
        inline = {n: zf.read(n).decode() for n in zf.namelist()}
    r = client.post(
        "/api/v1/verify",
        data={
            "config": json.dumps(data["config"]),
            "compilerOutput": json.dumps(data["compilerOutput"]),
            "targetDeployed": data["target"],
            "sources": json.dumps(inline),
        },
    )
    assert r.status_code == 200
    assert r.json()["report"]["verdict"] == "EXACT"


def test_tar_gz_package_equivalent(client, examples):
    r = submit_dir(client, examples / "01-exact", "sources.tar.gz")
    assert r.status_code == 200
    assert r.json()["report"]["verdict"] == "EXACT"


def test_each_check_records_real_hashes(client, examples):
    r = submit_dir(client, examples / "01-exact")
    rep = r.json()["report"]
    src = [c for c in rep["checks"] if c["code"] == "SOURCE_DIGEST" and c["detail"].get("match")]
    assert len(src) >= 1
    for c in src:
        assert len(c["detail"]["provided_keccak256"]) == 64
        assert c["detail"]["provided_keccak256"] == c["detail"]["metadata_keccak256"].lstrip("0x")
    h = [c for c in rep["checks"] if c["code"] == "METADATA_EMBEDDED_HASH"][0]
    ipfs = [x for x in h["detail"]["checks"] if x["scheme"] == "ipfs"][0]
    assert ipfs["ok"] is True and len(ipfs["computed"]) == 64
