#!/usr/bin/env python3
"""Generate throwaway EVM test keys for local development ONLY.

Prints the private key, the checksum address, and a curl-ready JSON snippet.
Keys are generated locally with eth_account and never touch any network.
Do not fund them on a real chain.
"""
from __future__ import annotations

import argparse
import json

from eth_account import Account


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-n", "--count", type=int, default=3, help="how many keys")
    args = parser.parse_args()

    samples = []
    for i in range(1, args.count + 1):
        acct = Account.create()
        s = {
            "label": f"custodial-{i}",
            "private_key_hex": "0x" + bytes(acct.key).hex(),
            "address": acct.address,
        }
        samples.append(s)
        body = json.dumps(
            {
                "label": s["label"],
                "kind": "custodial",
                "private_key_hex": s["private_key_hex"],
            }
        )
        print(f"## key {i}")
        print(f"private_key_hex: {s['private_key_hex']}")
        print(f"address:         {s['address']}")
        print(
            "create wallet:   curl -s -X POST localhost:8080/wallets "
            "-H \"Authorization: Bearer $API_KEY\" "
            "-H 'Content-Type: application/json' "
            f"-d '{body}'"
        )
        print()

    path = "test-keys.sample.json"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(samples, fh, indent=2)
    print(f"wrote {path} (disposable local-test sample; never fund these keys)")


if __name__ == "__main__":
    main()
