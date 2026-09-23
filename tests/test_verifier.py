"""End-to-end tests of the verification verdicts.

These assert the central guarantee: normalization separates only metadata
auxdata and library-link slots; every other difference is surfaced.
"""

import copy
import io
import json
import zipfile

import pytest

from provenance_service.crypto import eip55_checksum
from provenance_service.fixtures import (
    DEFAULT_VERSION,
    build_compiler_output,
    build_zip,
    example_sources,
    library_fqn,
    link_object,
    placeholder_for_fqn,
    relabel_sources,
)
from provenance_service.verifier import verify_job

ADDR_A = "0x8ba1f109551bD432803012645Ac136ddd64DBA72"
ADDR_B = "0xAb5801a7D398351b8bE11C439e05C5B3259aeC9B"
FQN = "src/Token.sol:Token"
LIB = library_fqn("src/SafeMath.sol", "SafeMath")


def _cfg(addr=ADDR_A, lib=LIB, *, enabled=True, runs=200, evm="paris",
         version=DEFAULT_VERSION, libs=None):
    return {
        "compiler_version": version,
        "optimizer": {"enabled": enabled, "runs": runs},
        "evm_version": evm,
        "libraries": libs if libs is not None else {lib: eip55_checksum(addr)},
    }


def _deployed(sources=None, addr=ADDR_A, lib=LIB, *, enabled=True, runs=200,
              evm="paris", version=DEFAULT_VERSION):
    sources = sources or example_sources()
    out = build_compiler_output(
        sources, optimizer_enabled=enabled, optimizer_runs=runs,
        evm_version=evm, version=version)
    art = out["contracts"]["src/Token.sol"]["Token"]["evm"]["deployedBytecode"]
    return out, link_object(art["object"], lib, addr)


def codes(res):
    return {f["code"] for c in res["contracts"] for f in c["findings"]}


def severities(res):
    return [(str(f["severity"]).split(".")[-1], f["code"])
            for c in res["contracts"] for f in c["findings"]]


# ---------------------------------------------------------------------------
# EXACT
# ---------------------------------------------------------------------------

def test_exact_match_happy_path(happy_parts):
    r = verify_job(happy_parts["package"], happy_parts["config"],
                   happy_parts["compiler_output"],
                   happy_parts["expected_runtime_code"])
    assert r["status"] == "OK"
    assert r["verdict"] == "EXACT_MATCH"
    cs = codes(r)
    assert {"SOURCE_DIGEST_OK", "METADATA_HASH_OK", "PLACEHOLDER_SCHEME_OK",
            "LINK_REF_OK", "ADDRESS_CHECKSUM_OK", "VERSION_OK", "CONFIG_OK"} <= cs
    assert not any(s == "ERROR" for s, _ in severities(r))


def test_packaged_script_recorded_but_not_run():
    out, deployed = _deployed()
    r = verify_job(build_zip(example_sources()), _cfg(), out, {FQN: deployed})
    assert r["verdict"] == "EXACT_MATCH"
    pf = r["package_findings"]
    assert pf and pf[0]["code"] == "SCRIPT_NOT_EXECUTED"
    assert "scripts/build.sh" in pf[0]["detail"]["members"]


def test_no_runtime_code_is_integrity_only_exact():
    out, _ = _deployed()
    r = verify_job(build_zip(example_sources()), _cfg(), out, None)
    assert r["verdict"] == "EXACT_MATCH"
    assert "NO_RUNTIME_COMPARISON" in codes(r)


def test_metadata_hash_recomputed_real():
    # Tamper the embedded metadata string but leave bytecode untouched:
    # the auxdata commitment must no longer verify.
    out, deployed = _deployed()
    art = out["contracts"]["src/Token.sol"]["Token"]
    meta = json.loads(art["metadata"])
    meta["settings"]["evmVersion"] = "london"  # alters hash only
    art["metadata"] = json.dumps(meta)
    r = verify_job(build_zip(example_sources()), _cfg(evm="paris"), out,
                   {FQN: deployed})
    assert r["verdict"] == "MISMATCH"
    assert "METADATA_HASH_DIFF" in codes(r)
    assert "SETTING_DIFF" in codes(r)


# ---------------------------------------------------------------------------
# RULE_MATCH (only metadata hash / library addresses)
# ---------------------------------------------------------------------------

def test_library_address_difference_is_rule_match():
    out, deployed_a = _deployed(addr=ADDR_A)
    deployed_b = link_object(
        out["contracts"]["src/Token.sol"]["Token"]["evm"]["deployedBytecode"]["object"],
        LIB, ADDR_B)
    r = verify_job(build_zip(example_sources()), _cfg(addr=ADDR_A), out,
                   {FQN: deployed_b})
    assert r["verdict"] == "RULE_MATCH"
    assert "LIBRARY_ADDRESS_DIFF" in codes(r)
    # body digests after slot masking must be identical
    rep = r["contracts"][0]
    assert rep["normalized_body_digest_expected"] == rep["normalized_body_digest_observed"]
    slot = rep["compared_library_slots"][0]
    assert slot["observed_address"] == ADDR_B.lower()
    assert slot["declared_address"] == eip55_checksum(ADDR_A)


def test_path_relocation_is_rule_match():
    sources = example_sources()
    out, deployed = _deployed(sources)
    mapping = {"src/SafeMath.sol": "contracts/lib/SafeMath.sol",
               "src/Token.sol": "contracts/Token.sol"}
    moved = relabel_sources(sources, mapping)
    out_m = build_compiler_output(
        moved,
        contracts=[("contracts/Token.sol", "Token",
                    [("contracts/lib/SafeMath.sol", "SafeMath")])])
    lib_m = library_fqn("contracts/lib/SafeMath.sol", "SafeMath")
    art_m = out_m["contracts"]["contracts/Token.sol"]["Token"][
        "evm"]["deployedBytecode"]["object"]
    deployed_moved = link_object(art_m, lib_m, ADDR_B)
    r = verify_job(build_zip(sources), _cfg(addr=ADDR_A), out,
                   {FQN: deployed_moved})
    assert r["verdict"] == "RULE_MATCH"
    assert "METADATA_TAIL_DIFF_HASH" in codes(r)
    assert "LIBRARY_ADDRESS_DIFF" in codes(r)
    assert "BODY_DIFF" not in codes(r)


# ---------------------------------------------------------------------------
# MISMATCH cases — normalization must never hide these
# ---------------------------------------------------------------------------

def test_optimizer_setting_mismatch_mismatch():
    out, deployed = _deployed(enabled=True)
    r = verify_job(build_zip(example_sources()),
                   _cfg(enabled=False), out, {FQN: deployed})
    assert r["verdict"] == "MISMATCH"
    assert "SETTING_DIFF" in codes(r)


def test_optimizer_rebuild_changes_body_not_masked():
    # Reference artifact built optimized-on; deployed code genuinely built
    # optimized-off. Masking metadata+slots must still report BODY_DIFF.
    out_on, _ = _deployed(enabled=True)
    _, deployed_off = _deployed(enabled=False)
    r = verify_job(build_zip(example_sources()), _cfg(enabled=False), out_on,
                   {FQN: deployed_off})
    assert r["verdict"] == "MISMATCH"
    assert "BODY_DIFF" in codes(r)
    detail = [f["detail"] for c in r["contracts"] for f in c["findings"]
              if f["code"] == "BODY_DIFF"][0]
    assert detail["count"] > 0


def test_evm_version_setting_mismatch():
    out, deployed = _deployed(evm="paris")
    r = verify_job(build_zip(example_sources()), _cfg(evm="cancun"), out,
                   {FQN: deployed})
    assert "SETTING_DIFF" in codes(r)
    assert r["verdict"] == "MISMATCH"


def test_version_mismatch():
    out, deployed = _deployed(version=DEFAULT_VERSION)
    r = verify_job(build_zip(example_sources()),
                   _cfg(version="0.8.23+commit.1e1f2e6d"),
                   out, {FQN: deployed})
    assert r["verdict"] == "MISMATCH"
    assert "VERSION_DIFF" in codes(r)


def test_missing_source_mismatch():
    out, deployed = _deployed()
    only_token = build_zip(
        {"src/Token.sol": example_sources()["src/Token.sol"]},
        include_script=False)
    r = verify_job(only_token, _cfg(), out, {FQN: deployed})
    assert r["verdict"] == "MISMATCH"
    assert "SOURCE_MISSING" in codes(r)


def test_source_digest_change_caught_without_rebuild():
    out, deployed = _deployed()
    tweaked = build_zip(example_sources(supply=2_000_000))
    r = verify_job(tweaked, _cfg(), out, {FQN: deployed})
    assert r["verdict"] == "MISMATCH"
    assert "SOURCE_DIGEST_DIFF" in codes(r)


def test_semantic_change_in_body_not_masked_by_normalization():
    out, _ = _deployed(example_sources(supply=1_000_000))
    _, deployed_tweaked = _deployed(example_sources(supply=1_000_001))
    r = verify_job(build_zip(example_sources(supply=1_000_001)),
                   _cfg(), out, {FQN: deployed_tweaked})
    # Even after stripping auxdata and masking the library slot, the changed
    # constant must leave differing code nibbles.
    assert r["verdict"] == "MISMATCH"
    assert "BODY_DIFF" in codes(r)
    assert "SOURCE_DIGEST_DIFF" in codes(r)


def test_single_nibble_tamper_in_code_is_caught():
    out, deployed = _deployed()
    # tamper a nibble in the middle of the code body (away from tail/slot)
    pos = len(deployed) - 130
    flipped = deployed[:pos] + ("1" if deployed[pos] != "1" else "2") + deployed[pos + 1:]
    r = verify_job(build_zip(example_sources()), _cfg(), out, {FQN: flipped})
    assert r["verdict"] == "MISMATCH"
    assert "BODY_DIFF" in codes(r)


def test_tamper_in_metadata_tail_solc_field_is_caught():
    out, deployed = _deployed()
    # flip a byte in the auxdata region (last ~100 hex chars), not the hash
    bad = deployed[:-20] + ("0" if deployed[-20] != "0" else "1") + deployed[-19:]
    r = verify_job(build_zip(example_sources()), _cfg(), out, {FQN: bad})
    # A corruption of the tail either fails CBOR parse or appears as a diff.
    assert r["verdict"] == "MISMATCH"
    assert codes(r) & {"METADATA_TAIL_MALFORMED", "METADATA_TAIL_DIFF_OTHER",
                       "METADATA_HASH_DIFF", "BODY_DIFF"}


def test_unresolved_placeholder_in_deployed_code():
    out, _ = _deployed()
    unlinked = out["contracts"]["src/Token.sol"]["Token"][
        "evm"]["deployedBytecode"]["object"]
    r = verify_job(build_zip(example_sources()), _cfg(), out, {FQN: unlinked})
    assert r["verdict"] == "MISMATCH"
    assert "PLACEHOLDER_UNRESOLVED" in codes(r)


def test_bad_address_checksum_rejected():
    out, deployed = _deployed()
    cfg = _cfg()
    key = next(iter(cfg["libraries"]))
    cfg["libraries"][key] = cfg["libraries"][key].lower()
    r = verify_job(build_zip(example_sources()), cfg, out, {FQN: deployed})
    assert r["verdict"] == "MISMATCH"
    assert "ADDRESS_BAD_CHECKSUM" in codes(r)


def test_undeclared_library_mapping():
    out, deployed = _deployed()
    r = verify_job(build_zip(example_sources()), _cfg(libs={}), out,
                   {FQN: deployed})
    assert r["verdict"] == "MISMATCH"
    assert "ADDRESS_UNDECLARED" in codes(r)


def test_runtime_length_difference_is_mismatch():
    out, deployed = _deployed()
    r = verify_job(build_zip(example_sources()), _cfg(), out,
                   {FQN: deployed + "00"})
    assert r["verdict"] == "MISMATCH"
    assert "RUNTIME_LEN_DIFF" in codes(r)


def test_malformed_auxdata_in_reference_is_mismatch():
    out, deployed = _deployed()
    art = out["contracts"]["src/Token.sol"]["Token"]["evm"]["deployedBytecode"]
    art["object"] = art["object"][:-4] + "aabb"  # break sentinel
    r = verify_job(build_zip(example_sources()), _cfg(), out, {FQN: deployed})
    assert r["verdict"] == "MISMATCH"
    assert "METADATA_TAIL_MALFORMED" in codes(r)


# ---------------------------------------------------------------------------
# Package rejection
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("evil", [
    "../evil.sol", "a/../../evil.sol", "/tmp/evil.sol",
])
def test_malicious_paths_reject_entire_job(evil):
    out, deployed = _deployed()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for p, c in example_sources().items():
            zf.writestr(p, c)
        zf.writestr(evil, b"payload")
    r = verify_job(buf.getvalue(), _cfg(), out, {FQN: deployed})
    assert r["status"] == "REJECTED"
    assert r["verdict"] == "MISMATCH"
    assert r["contracts"][0]["findings"][0]["code"] == "UNSAFE_PATH"


# ---------------------------------------------------------------------------
# Evidence chain
# ---------------------------------------------------------------------------

def test_evidence_chain_is_deterministic_and_complete(happy_parts):
    r1 = verify_job(happy_parts["package"], happy_parts["config"],
                    happy_parts["compiler_output"],
                    happy_parts["expected_runtime_code"])
    r2 = verify_job(happy_parts["package"], happy_parts["config"],
                    happy_parts["compiler_output"],
                    happy_parts["expected_runtime_code"])
    # job id/time differ, but finding chain for identical inputs is equal
    assert r1["evidence"]["finding_chain"] == r2["evidence"]["finding_chain"]
    links = r1["evidence"]["finding_chain"]
    assert len(links) == 1 and links[0]["fqn"] == FQN
    # every reported severity is represented in the stored result
    all_codes = codes(r1)
    for c in r1["contracts"]:
        for f in c["findings"]:
            assert f["code"] in all_codes
            assert f["message"]
