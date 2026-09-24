#!/usr/bin/env python3
"""Generate a fixed local demo PKI into ``examples/certs/``.

Run from the repository root:

    .venv/bin/python scripts/gen_certs.py

Produces a valid chain plus deliberately broken fixtures used by the curl
examples in the README. Nothing here is secret - keys are generated only so
the certificates can be re-issued/chained locally.
"""

from __future__ import annotations

import datetime as dt
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from cryptography.hazmat.primitives import serialization  # noqa: E402

from tests.certfactory import (  # noqa: E402
    CLIENT_AUTH,
    SERVER_AUTH,
    build_cert,
    generate_key,
    key_pem,
    make_intermediate,
    make_leaf,
    make_root,
    pem,
)

OUT = pathlib.Path(__file__).resolve().parents[1] / "examples" / "certs"


def write(name: str, cert, key=None) -> None:
    (OUT / f"{name}.crt").write_text(pem(cert.cert))
    if key is not None:
        (OUT / f"{name}.key").write_text(key_pem(cert.key))


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now(dt.timezone.utc)

    # --- Good PKI -------------------------------------------------------
    root = make_root("Demo Root CA")
    inter = make_intermediate(root, "Demo Intermediate CA")
    leaf = make_leaf(inter, "demo.example.com",
                     dns_names=("demo.example.com", "www.demo.example.com"))
    write("root", root)
    write("intermediate", inter)
    write("leaf_valid", leaf)

    # --- Client-auth leaf (EKU clientAuth) -------------------------------
    client = make_leaf(inter, "client.demo.example.com",
                       dns_names=("client.demo.example.com",),
                       eku=[CLIENT_AUTH])
    write("leaf_client", client)

    # --- Expired leaf ---------------------------------------------------
    expired = make_leaf(
        inter, "expired.demo.example.com",
        dns_names=("expired.demo.example.com",),
        not_before=now - dt.timedelta(days=60),
        not_after=now - dt.timedelta(days=1),
    )
    write("leaf_expired", expired)

    # --- Intermediate that is not a CA (cA=false) -----------------------
    non_ca = make_intermediate(root, "Not A CA Intermediate", is_ca=False)
    under_non_ca = make_leaf(non_ca, "bad-ca.demo.example.com",
                             dns_names=("bad-ca.demo.example.com",))
    write("intermediate_non_ca", non_ca)
    write("leaf_under_non_ca", under_non_ca)

    # --- Name mismatch (SAN does not contain the requested name) --------
    wrong_name = make_leaf(inter, "other.example.net",
                           dns_names=("other.example.net",))
    write("leaf_wrong_name", wrong_name)

    # --- Self-signed cert NOT present in the trust store ----------------
    rogue_key = generate_key()
    rogue = build_cert(
        "rogue.example.com", issuer=None, signing_key=rogue_key, is_ca=False,
        dns_names=("rogue.example.com",), eku=[SERVER_AUTH],
    )
    (OUT / "rogue_selfsigned.crt").write_text(
        rogue.public_bytes(serialization.Encoding.PEM).decode("ascii"))
    (OUT / "rogue_selfsigned.key").write_text(key_pem(rogue_key))

    # --- A separate attacker root + chain (root only offered as "intermediate") ---
    attacker_root = make_root("Attacker Root CA")
    attacker_inter = make_intermediate(attacker_root, "Attacker Intermediate CA")
    attacker_leaf = make_leaf(attacker_inter, "attacker.example.com",
                              dns_names=("attacker.example.com",))
    write("attacker_root", attacker_root)
    write("attacker_intermediate", attacker_inter)
    write("attacker_leaf", attacker_leaf)

    print(f"Wrote demo fixtures to {OUT}/")
    for p in sorted(OUT.iterdir()):
        print(" ", p.name)


if __name__ == "__main__":
    main()
