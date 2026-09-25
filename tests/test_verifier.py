"""核心验证逻辑测试：覆盖验收要求的各类链。"""
import pytest

from certchain.fixtures import (
    ALL_CASES,
    DEFAULT_VALIDATION_TIME,
    case_expired,
    case_good,
    case_non_ca_intermediate,
    case_pathlen_violation,
    case_same_name,
)
from certchain.verifier import REVOCATION_NOTE, VerifyInputError, verify_chain


def _run(case, **overrides):
    kwargs = dict(
        leaf_pem=case["leaf"],
        intermediates_pem=case["intermediates"],
        trust_roots_pem=case["trust_roots"],
        validation_time_iso=DEFAULT_VALIDATION_TIME,
        purpose="server_tls",
        hostname=case["hostname"],
    )
    kwargs.update(overrides)
    return verify_chain(**kwargs)


def test_good_chain_valid():
    res = _run(case_good())
    assert res["valid"] is True, res["error"]
    # 链应包含 叶子 -> 中间 -> 根
    assert len(res["chain"]) == 3
    assert res["chain"][0]["is_ca"] is False
    assert res["chain"][1]["is_ca"] is True
    assert res["chain"][2]["is_ca"] is True
    assert res["hostname"] == "service.example.local"


def test_revocation_explicitly_not_checked():
    res = _run(case_good())
    assert res["revocation_checked"] is False
    assert res["revocation_note"] == REVOCATION_NOTE
    assert "吊销" in res["revocation_note"]


def test_expired_chain_rejected():
    res = _run(case_expired())
    assert res["valid"] is False
    assert res["error"]


def test_expired_chain_accepted_at_earlier_time():
    """显式验证时刻：同一过期链在 2020 年验证应通过。"""
    res = _run(case_expired(), validation_time_iso="2020-06-01T00:00:00Z")
    assert res["valid"] is True, res["error"]


def test_good_chain_rejected_after_validity():
    """显式验证时刻：good 链在 2031 年（超出有效期）应失败。"""
    res = _run(case_good(), validation_time_iso="2031-01-01T00:00:00Z")
    assert res["valid"] is False


def test_pathlen_violation_rejected():
    res = _run(case_pathlen_violation())
    assert res["valid"] is False
    assert res["error"]


def test_non_ca_intermediate_rejected():
    res = _run(case_non_ca_intermediate())
    assert res["valid"] is False
    assert res["error"]


def test_same_name_chain_builds_correct_path():
    """同名中间证书并存时，路径构建必须按密钥选中正确的一张。"""
    case = case_same_name()
    res = _run(case)
    assert res["valid"] is True, res["error"]
    # 选中的中间证书必须是由 root1 签发的（subject 同名，靠链关系区分）
    assert len(res["chain"]) == 3


def test_same_name_wrong_intermediate_only_rejected():
    """只提供同名异钥的中间证书时，验证必须失败。"""
    case = case_same_name()
    res = _run(case, intermediates_pem=case["intermediates_wrong_only"])
    assert res["valid"] is False
    assert res["error"]


def test_hostname_mismatch_rejected():
    res = _run(case_good(), hostname="other.example.local")
    assert res["valid"] is False
    assert res["error"]


def test_untrusted_root_rejected():
    """信任根换成同名异钥的另一棵根，验证必须失败。"""
    case = case_same_name()
    res = _run(case, trust_roots_pem=case["trust_roots_other"])
    assert res["valid"] is False


def test_purpose_client_tls_rejected_for_server_only_leaf():
    """显式用途：叶子只有 serverAuth EKU，按 client_tls 验证必须失败。"""
    res = _run(case_good(), purpose="client_tls", hostname=None)
    assert res["valid"] is False
    assert res["error"]


def test_missing_validation_time():
    with pytest.raises(VerifyInputError):
        _run(case_good(), validation_time_iso=None)


def test_missing_hostname_for_server_purpose():
    with pytest.raises(VerifyInputError):
        _run(case_good(), hostname=None)


def test_bad_purpose():
    with pytest.raises(VerifyInputError):
        _run(case_good(), purpose="email")


def test_bad_pem():
    with pytest.raises(VerifyInputError):
        _run(case_good(), leaf_pem="not a pem")


def test_empty_trust_roots():
    with pytest.raises(VerifyInputError):
        _run(case_good(), trust_roots_pem=[])


def test_naive_time_treated_as_utc():
    res = _run(case_good(), validation_time_iso="2026-06-01T00:00:00")
    assert res["valid"] is True, res["error"]
    assert res["validation_time"].endswith("+00:00")


def test_all_cases_generate():
    assert set(ALL_CASES) == {
        "good",
        "expired",
        "pathlen_violation",
        "non_ca_intermediate",
        "same_name",
    }
