"""Acceptance scenarios for the authoritative offline verifier."""

from __future__ import annotations

import datetime as dt

import pytest

from app.verifier import PURPOSE_CLIENT, PURPOSE_SERVER, VerificationInputError, verify_chain

from .conftest import as_pem  # noqa: F401  (kept for symmetry / future reuse)


def verify(pki, leaf, *, subject="example.com", extras=None, roots=None,
           purpose=PURPOSE_SERVER, **kw):
    return verify_chain(
        leaf.cert,
        list(extras or []),
        [r.cert for r in (roots or [pki["root"]])],
        purpose=purpose,
        subject=subject,
        **kw,
    )


class TestHappyPath:
    def test_valid_chain(self, pki):
        r = verify(pki, pki["leaf"], extras=[pki["intermediate"].cert])
        assert r.valid is True
        assert r.error_code is None
        roles = [c["role"] for c in r.chain]
        assert roles == ["leaf", "intermediate", "root"]
        assert r.chain[-1]["subject_cn"] == "Good Root CA"
        assert any(f["code"] == "VERIFIED" for f in r.findings)

    def test_valid_chain_rsa(self, pki):
        from .certfactory import make_intermediate, make_leaf, make_root
        root = make_root("RSA Root", key_type="rsa")
        inter = make_intermediate(root, "RSA Inter", key_type="rsa")
        leaf = make_leaf(inter, "rsa.example.com",
                         dns_names=("rsa.example.com",), key_type="rsa")
        r = verify_chain(
            leaf.cert, [inter.cert], [root.cert],
            purpose=PURPOSE_SERVER, subject="rsa.example.com",
        )
        assert r.valid

    def test_wildcard_san(self, pki):
        from .certfactory import make_leaf
        leaf = make_leaf(pki["intermediate"], "wc", dns_names=("*.example.com",))
        r = verify(pki, leaf, subject="foo.example.com",
                   extras=[pki["intermediate"].cert])
        assert r.valid

    def test_ip_subject(self, pki, ip_chain):
        r = verify_chain(
            ip_chain.cert, [pki["intermediate"].cert], [pki["root"].cert],
            purpose=PURPOSE_SERVER, subject="10.20.30.40",
        )
        assert r.valid

    def test_client_auth_purpose(self, pki):
        r = verify_chain(
            pki["client_leaf"].cert, [pki["intermediate"].cert], [pki["root"].cert],
            purpose=PURPOSE_CLIENT,
        )
        assert r.valid
        assert any(f["code"] == "CLIENT_SUBJECTS" for f in r.findings)

    def test_verification_time_within_validity(self, pki, expired_leaf, now):
        # The expired leaf WAS valid 30 days ago (nb = now-60, na = now-1).
        r = verify_chain(
            expired_leaf.cert, [pki["intermediate"].cert], [pki["root"].cert],
            purpose=PURPOSE_SERVER, subject="expired.example.com",
            verification_time=now - dt.timedelta(days=30),
        )
        assert r.valid
        assert r.verification_time == (now - dt.timedelta(days=30)).isoformat()


class TestValidityPeriod:
    def test_expired_leaf(self, pki, expired_leaf):
        r = verify(pki, expired_leaf, subject="expired.example.com",
                   extras=[pki["intermediate"].cert])
        assert not r.valid
        assert r.error_code == "VALIDITY_PERIOD"
        assert any(f["code"] == "CERT_EXPIRED" and f["role"] == "leaf"
                   for f in r.findings)

    def test_not_yet_valid_leaf(self, pki, future_leaf):
        r = verify(pki, future_leaf, subject="future.example.com",
                   extras=[pki["intermediate"].cert])
        assert not r.valid
        assert r.error_code == "VALIDITY_PERIOD"
        assert any(f["code"] == "CERT_NOT_YET_VALID" for f in r.findings)

    def test_expired_intermediate(self, pki, expired_intermediate_chain):
        inter, leaf = expired_intermediate_chain
        r = verify(pki, leaf, subject="under-expired.example.com",
                   extras=[inter.cert])
        assert not r.valid
        assert r.error_code == "VALIDITY_PERIOD"
        assert any(f["code"] == "CERT_EXPIRED" and f["role"] == "intermediate"
                   for f in r.findings)

    def test_expired_root_not_trusted(self, expired_root_chain):
        root, inter, leaf = expired_root_chain
        r = verify_chain(
            leaf.cert, [inter.cert], [root.cert],
            purpose=PURPOSE_SERVER, subject="under-expired-root.example.com",
        )
        assert not r.valid
        assert r.error_code in ("VALIDITY_PERIOD", "UNTRUSTED_CHAIN")
        assert any(f["code"] == "CERT_EXPIRED" and f["role"] == "root"
                   for f in r.findings)


class TestNameMatching:
    def test_dns_name_mismatch(self, pki, name_mismatch_leaf):
        r = verify(pki, name_mismatch_leaf, subject="example.com",
                   extras=[pki["intermediate"].cert])
        assert not r.valid
        assert r.error_code == "NAME_MISMATCH"
        assert any(f["code"] == "NAME_MISMATCH" for f in r.findings)

    def test_wildcard_does_not_cross_label_boundary(self, pki):
        from .certfactory import make_leaf
        leaf = make_leaf(pki["intermediate"], "wc", dns_names=("*.example.com",))
        for bad in ("example.com", "a.b.example.com"):
            r = verify(pki, leaf, subject=bad,
                       extras=[pki["intermediate"].cert])
            assert not r.valid and r.error_code == "NAME_MISMATCH", bad

    def test_ip_not_matched_by_dns_san(self, pki):
        r = verify_chain(
            pki["leaf"].cert, [pki["intermediate"].cert], [pki["root"].cert],
            purpose=PURPOSE_SERVER, subject="10.20.30.40",
        )
        assert not r.valid
        assert r.error_code == "NAME_MISMATCH"


class TestCAProperties:
    def test_non_ca_intermediate_rejected(self, pki, non_ca_intermediate_chain):
        non_ca, leaf = non_ca_intermediate_chain
        r = verify(pki, leaf, subject="under-nonca.example.com",
                   extras=[non_ca.cert])
        assert not r.valid
        assert r.error_code in ("NOT_A_CA", "REQUIRED_EXTENSION")
        assert "basicConstraints" in r.error_message or "extension" in r.error_message

    def test_pathlen_zero_violated(self, pathlen_chains):
        (root0, inter0, leaf0), _ = pathlen_chains
        r = verify_chain(
            leaf0.cert, [inter0.cert], [root0.cert],
            purpose=PURPOSE_SERVER, subject="p0.example.com",
        )
        assert not r.valid
        assert r.error_code == "PATH_LEN_CONSTRAINT"

    def test_pathlen_one_ok(self, pathlen_chains):
        _, (root1, inter1, leaf1) = pathlen_chains
        r = verify_chain(
            leaf1.cert, [inter1.cert], [root1.cert],
            purpose=PURPOSE_SERVER, subject="p1.example.com",
        )
        assert r.valid


class TestTrustAnchor:
    def test_untrusted_self_signed_leaf(self, pki, untrusted_self_signed):
        r = verify_chain(
            untrusted_self_signed.cert, [], [pki["root"].cert],
            purpose=PURPOSE_SERVER, subject="selfsigned.example.com",
        )
        assert not r.valid
        assert r.error_code == "UNTRUSTED_CHAIN"

    def test_self_signed_root_in_intermediates_is_not_anchor(
        self, pki, untrusted_root_chain
    ):
        """The attacker root is offered ONLY as an 'intermediate'; trust store
        contains an unrelated good root. It must never terminate the chain."""
        bad_root, bad_inter, bad_leaf = untrusted_root_chain
        r = verify_chain(
            bad_leaf.cert,
            [bad_inter.cert, bad_root.cert],
            [pki["root"].cert],
            purpose=PURPOSE_SERVER, subject="attacker.example.com",
        )
        assert not r.valid
        assert r.error_code in ("UNTRUSTED_CHAIN", "MAX_CHAIN_DEPTH")

    def test_chain_under_untrusted_root_rejected(self, pki, untrusted_root_chain):
        bad_root, bad_inter, bad_leaf = untrusted_root_chain
        r = verify_chain(
            bad_leaf.cert, [bad_inter.cert], [pki["root"].cert],
            purpose=PURPOSE_SERVER, subject="attacker.example.com",
        )
        assert not r.valid

    def test_same_root_explicitly_trusted_then_ok(self, untrusted_root_chain):
        root, inter, leaf = untrusted_root_chain
        r = verify_chain(
            leaf.cert, [inter.cert], [root.cert],
            purpose=PURPOSE_SERVER, subject="attacker.example.com",
        )
        assert r.valid

    def test_root_only_in_store_no_intermediate(self, pki):
        r = verify_chain(
            pki["leaf"].cert, [], [pki["root"].cert],
            purpose=PURPOSE_SERVER, subject="example.com",
        )
        assert not r.valid
        assert r.error_code == "UNTRUSTED_CHAIN"


class TestEkuAndDepth:
    def test_server_purpose_rejects_client_auth_leaf(self, pki):
        # Same SAN as the name requested: the EKU check is what must fire.
        r = verify_chain(
            pki["client_leaf"].cert, [pki["intermediate"].cert], [pki["root"].cert],
            purpose=PURPOSE_SERVER, subject="client-1.example",
        )
        assert not r.valid
        assert r.error_code == "EKU"

    def test_client_purpose_rejects_server_auth_leaf(self, pki):
        r = verify_chain(
            pki["leaf"].cert, [pki["intermediate"].cert], [pki["root"].cert],
            purpose=PURPOSE_CLIENT,
        )
        assert not r.valid
        assert r.error_code == "EKU"

    def test_max_chain_depth_zero(self, pki):
        r = verify_chain(
            pki["leaf"].cert, [pki["intermediate"].cert], [pki["root"].cert],
            purpose=PURPOSE_SERVER, subject="example.com",
            max_chain_depth=0,
        )
        assert not r.valid
        assert r.error_code == "MAX_CHAIN_DEPTH"

    def test_max_chain_depth_one_ok(self, pki):
        r = verify_chain(
            pki["leaf"].cert, [pki["intermediate"].cert], [pki["root"].cert],
            purpose=PURPOSE_SERVER, subject="example.com",
            max_chain_depth=1,
        )
        assert r.valid


class TestInputValidation:
    def test_empty_trust_store_rejected(self, pki):
        with pytest.raises(VerificationInputError):
            verify_chain(
                pki["leaf"].cert, [pki["intermediate"].cert], [],
                purpose=PURPOSE_SERVER, subject="example.com",
            )

    def test_server_requires_subject(self, pki):
        with pytest.raises(VerificationInputError):
            verify_chain(
                pki["leaf"].cert, [pki["intermediate"].cert], [pki["root"].cert],
                purpose=PURPOSE_SERVER,
            )

    def test_bad_purpose(self, pki):
        with pytest.raises(VerificationInputError):
            verify_chain(
                pki["leaf"].cert, [pki["intermediate"].cert], [pki["root"].cert],
                purpose="email", subject="example.com",
            )

    def test_naive_verification_time_rejected(self, pki):
        with pytest.raises(VerificationInputError):
            verify_chain(
                pki["leaf"].cert, [pki["intermediate"].cert], [pki["root"].cert],
                purpose=PURPOSE_SERVER, subject="example.com",
                verification_time="2026-09-24T12:00:00",
            )

    def test_negative_depth_rejected(self, pki):
        with pytest.raises(VerificationInputError):
            verify_chain(
                pki["leaf"].cert, [pki["intermediate"].cert], [pki["root"].cert],
                purpose=PURPOSE_SERVER, subject="example.com",
                max_chain_depth=-1,
            )
