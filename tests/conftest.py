"""Shared fixtures: a small PKI plus every deliberately-broken variant.

Timeline: "now" is real wall-clock UTC; expired fixtures are anchored relative
to now so the suite stays correct over time.
"""

from __future__ import annotations

import datetime as dt

import pytest

from .certfactory import (
    CLIENT_AUTH,
    SERVER_AUTH,
    build_cert,
    generate_key,
    make_intermediate,
    make_leaf,
    make_root,
    pem,
)


@pytest.fixture(scope="session")
def now():
    return dt.datetime.now(dt.timezone.utc)


@pytest.fixture(scope="session")
def pki():
    """A valid root -> intermediate -> server/leaf chain."""
    root = make_root("Good Root CA")
    inter = make_intermediate(root, "Good Intermediate CA")
    leaf = make_leaf(inter, "example.com", dns_names=("example.com", "www.example.com"))
    client_leaf = make_leaf(
        inter, "client-1", dns_names=("client-1.example",), eku=[CLIENT_AUTH]
    )
    return {
        "root": root,
        "intermediate": inter,
        "leaf": leaf,
        "client_leaf": client_leaf,
    }


@pytest.fixture(scope="session")
def expired_leaf(pki, now):
    return make_leaf(
        pki["intermediate"], "expired.example.com",
        dns_names=("expired.example.com",),
        not_before=now - dt.timedelta(days=60),
        not_after=now - dt.timedelta(days=1),
    )


@pytest.fixture(scope="session")
def future_leaf(pki, now):
    return make_leaf(
        pki["intermediate"], "future.example.com",
        dns_names=("future.example.com",),
        not_before=now + dt.timedelta(days=10),
        not_after=now + dt.timedelta(days=40),
    )


@pytest.fixture(scope="session")
def expired_intermediate_chain(pki, now):
    """Chain whose intermediate is already expired."""
    inter = make_intermediate(
        pki["root"], "Expired Intermediate CA",
        not_before=now - dt.timedelta(days=400),
        not_after=now - dt.timedelta(days=10),
    )
    leaf = make_leaf(inter, "under-expired.example.com",
                     dns_names=("under-expired.example.com",))
    return inter, leaf


@pytest.fixture(scope="session")
def expired_root_chain(now):
    """Chain whose self-signed root is itself expired."""
    root = make_root(
        "Expired Root CA",
        not_before=now - dt.timedelta(days=4000),
        not_after=now - dt.timedelta(days=10),
    )
    inter = make_intermediate(root, "Inter under Expired Root")
    leaf = make_leaf(inter, "under-expired-root.example.com",
                     dns_names=("under-expired-root.example.com",))
    return root, inter, leaf


@pytest.fixture(scope="session")
def non_ca_intermediate_chain(pki):
    """A 'leaf-like' (cA=false) cert used as an intermediate."""
    non_ca = make_intermediate(pki["root"], "Not Actually A CA", is_ca=False)
    leaf = make_leaf(non_ca, "under-nonca.example.com",
                     dns_names=("under-nonca.example.com",))
    return non_ca, leaf


@pytest.fixture(scope="session")
def name_mismatch_leaf(pki):
    return make_leaf(pki["intermediate"], "wrong.example.net",
                     dns_names=("wrong.example.net",))


@pytest.fixture(scope="session")
def untrusted_self_signed(now):
    """Self-signed end-entity cert for a name; not in the trust store."""
    k = generate_key()
    c = build_cert(
        "selfsigned.example.com", issuer=None, signing_key=k, is_ca=False,
        dns_names=("selfsigned.example.com",), eku=[SERVER_AUTH],
    )
    return type("Issued", (), {"cert": c, "key": k, "name": "selfsigned"})()


@pytest.fixture(scope="session")
def untrusted_root_chain():
    """A fully valid chain under a root the verifier does NOT trust."""
    root = make_root("Attacker Root CA")
    inter = make_intermediate(root, "Attacker Intermediate CA")
    leaf = make_leaf(inter, "attacker.example.com",
                     dns_names=("attacker.example.com",))
    return root, inter, leaf


@pytest.fixture(scope="session")
def pathlen_chains():
    """root(pathLen=0/1) -> intermediate -> leaf."""
    root0 = make_root("Root pathLen0", pathlen=0)
    inter0 = make_intermediate(root0, "Inter under pathLen0")
    leaf0 = make_leaf(inter0, "p0.example.com", dns_names=("p0.example.com",))
    root1 = make_root("Root pathLen1", pathlen=1)
    inter1 = make_intermediate(root1, "Inter under pathLen1")
    leaf1 = make_leaf(inter1, "p1.example.com", dns_names=("p1.example.com",))
    return (root0, inter0, leaf0), (root1, inter1, leaf1)


@pytest.fixture(scope="session")
def ip_chain(pki):
    return make_leaf(pki["intermediate"], "ip-host",
                     dns_names=(), ip_names=("10.20.30.40",))


def as_pem(cert) -> str:
    return pem(cert.cert if hasattr(cert, "cert") else cert)
