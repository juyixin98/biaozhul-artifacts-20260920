"""End-to-end validation of every generated scenario via verify_chain()."""

from __future__ import annotations

from cryptography.hazmat.primitives.hashes import SHA256

from certverifier.fixtures import VALID_MOMENT
from certverifier.models import Purpose


def _codes(result):
    return {f.code for f in result.findings}


def test_good_chain_is_valid(scenarios, verify):
    sc = scenarios["good"]
    result = verify(
        sc.leaf, sc.all_intermediates, sc.anchors,
        verification_time=sc.verification_time,
        purpose=Purpose.SERVER_AUTH, hostname=sc.hostname,
    )
    assert result.valid, result.to_dict()
    assert _codes(result) == set()
    # chain includes leaf, intermediate and anchor
    subjects = [c["subject"] for c in result.chain]
    assert len(subjects) == 3
    assert result.trust_anchor is not None
    assert result.trust_anchor["is_trust_anchor"] is True


def test_wildcard_matches_one_label(scenarios, verify):
    sc = scenarios["wildcard"]
    ok = verify(
        sc.leaf, sc.all_intermediates, sc.anchors,
        verification_time=sc.verification_time, purpose=Purpose.SERVER_AUTH,
        hostname="api.example.test",
    )
    assert ok.valid, ok.to_dict()
    # Two labels must NOT match
    bad = verify(
        sc.leaf, sc.all_intermediates, sc.anchors,
        verification_time=sc.verification_time, purpose=Purpose.SERVER_AUTH,
        hostname="a.b.example.test",
    )
    assert not bad.valid
    assert "HOSTNAME_MISMATCH" in _codes(bad)


def test_expired_leaf_fails(scenarios, verify):
    sc = scenarios["expired"]
    result = verify(
        sc.leaf, sc.all_intermediates, sc.anchors,
        verification_time=sc.verification_time, purpose=sc.purpose,
        hostname=sc.hostname,
    )
    assert not result.valid
    assert "EXPIRED" in _codes(result)
    expired = [f for f in result.findings if f.code == "EXPIRED"]
    assert expired and expired[0].certificate_index == 0


def test_same_chain_valid_at_earlier_moment(scenarios, verify):
    """The explicit verification moment must drive the expiry decision."""
    sc = scenarios["expired"]
    from certverifier.fixtures import VALID_MOMENT
    result = verify(
        sc.leaf, sc.all_intermediates, sc.anchors,
        verification_time=VALID_MOMENT, purpose=sc.purpose,
        hostname=sc.hostname,
    )
    assert result.valid, result.to_dict()


def test_not_yet_valid_fails(scenarios, verify):
    sc = scenarios["not_yet_valid"]
    result = verify(
        sc.leaf, sc.all_intermediates, sc.anchors,
        verification_time=sc.verification_time, hostname=sc.hostname,
    )
    assert not result.valid
    assert "NOT_YET_VALID" in _codes(result)


def test_path_length_violation_fails(scenarios, verify):
    sc = scenarios["path_length_violation"]
    result = verify(
        sc.leaf, sc.all_intermediates, sc.anchors,
        verification_time=sc.verification_time, hostname=sc.hostname,
    )
    assert not result.valid
    assert "PATH_LENGTH_VIOLATION" in _codes(result)
    # Violation is attributed to the constraining root (last path entry).
    finding = next(f for f in result.findings if f.code == "PATH_LENGTH_VIOLATION")
    assert finding.certificate_index == 2  # root position in leaf->root order


def test_non_ca_intermediate_fails(scenarios, verify):
    sc = scenarios["non_ca_intermediate"]
    result = verify(
        sc.leaf, sc.all_intermediates, sc.anchors,
        verification_time=sc.verification_time, hostname=sc.hostname,
    )
    assert not result.valid
    assert "NOT_A_CA" in _codes(result)
    idx = next(f.certificate_index for f in result.findings if f.code == "NOT_A_CA")
    assert idx == 1  # the intermediate, not the leaf


def test_same_name_rogue_root_is_rejected(scenarios, verify):
    sc = scenarios["same_name_rogue_root"]

    real_root = sc.anchors[0]
    rogue_root = sc.untrusted_roots[0]
    # The two roots share an IDENTICAL subject distinguished name...
    assert real_root.subject == rogue_root.subject
    # ...but use different keys (different fingerprints).
    assert real_root.fingerprint(SHA256()) != rogue_root.fingerprint(SHA256())

    # Only the genuine root is trusted -> rogue same-name chain must fail.
    result = verify(
        sc.leaf, sc.all_intermediates, sc.anchors,
        verification_time=sc.verification_time, hostname=sc.hostname,
    )
    assert not result.valid
    assert "NO_PATH_TO_TRUST_ANCHOR" in _codes(result)

    # If an operator mistakenly added the same-named rogue root to the trust
    # store, the rogue chain would validate. This shows trust follows the key,
    # not the name -- and that the trust store content is the explicit control.
    result_trusting_rogue = verify(
        sc.leaf, sc.all_intermediates, [rogue_root],
        verification_time=sc.verification_time, hostname=sc.hostname,
    )
    assert result_trusting_rogue.valid, result_trusting_rogue.to_dict()


def test_same_name_intermediate_is_disambiguated_by_signature(scenarios, verify):
    sc = scenarios["same_name_intermediate"]
    result = verify(
        sc.leaf, sc.all_intermediates, sc.anchors,
        verification_time=sc.verification_time, hostname=sc.hostname,
    )
    assert not result.valid
    assert "NO_PATH_TO_TRUST_ANCHOR" in _codes(result)


def test_hostname_mismatch_fails(scenarios, verify):
    sc = scenarios["hostname_mismatch"]
    result = verify(
        sc.leaf, sc.all_intermediates, sc.anchors,
        verification_time=sc.verification_time, hostname=sc.hostname,
    )
    assert not result.valid
    assert "HOSTNAME_MISMATCH" in _codes(result)


def test_eku_mismatch_fails(scenarios, verify):
    sc = scenarios["eku_mismatch"]
    result = verify(
        sc.leaf, sc.all_intermediates, sc.anchors,
        verification_time=sc.verification_time, hostname=sc.hostname,
    )
    assert not result.valid
    assert "EKU_MISMATCH" in _codes(result)
    # Purpose=any must not trigger EKU checks.
    any_result = verify(
        sc.leaf, sc.all_intermediates, sc.anchors,
        verification_time=sc.verification_time, purpose=Purpose.ANY, hostname=None,
    )
    assert any_result.valid, any_result.to_dict()


def test_untrusted_root_fails(scenarios, verify):
    sc = scenarios["untrusted_root"]
    result = verify(
        sc.leaf, sc.all_intermediates, sc.anchors,
        verification_time=sc.verification_time, hostname=sc.hostname,
    )
    assert not result.valid
    assert "NO_PATH_TO_TRUST_ANCHOR" in _codes(result)


def test_expired_intermediate_fails(scenarios, verify):
    sc = scenarios["expired_intermediate"]
    result = verify(
        sc.leaf, sc.all_intermediates, sc.anchors,
        verification_time=sc.verification_time, hostname=sc.hostname,
    )
    assert not result.valid
    assert "EXPIRED" in _codes(result)
    finding = next(f for f in result.findings if f.code == "EXPIRED")
    assert finding.certificate_index == 1


def test_revocation_never_checked(scenarios, verify):
    """Acceptance requirement: revocation status must be explicit and false."""
    for name, sc in scenarios.items():
        result = verify(
            sc.leaf, sc.all_intermediates, sc.anchors,
            verification_time=sc.verification_time, purpose=sc.purpose,
            hostname=sc.hostname,
        )
        body = result.to_dict()
        assert body["revocation"]["checked"] is False, name
        assert body["revocation"]["crl_checked"] is False, name
        assert body["revocation"]["ocsp_checked"] is False, name
        assert "NOT checked" in body["revocation"]["note"], name
